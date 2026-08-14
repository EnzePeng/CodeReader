"""Security and project-boundary contract tests.

These tests intentionally avoid the model service.  They exercise the public
HTTP boundary and the filesystem capability that backs ``project_id``.
"""
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch


class ProjectRegistryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        self.outside = Path(self.tmp.name) / "outside.py"
        self.outside.write_text("secret\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_open_returns_opaque_id_and_resolves_relative_file(self) -> None:
        from app.projects import ProjectRegistry

        registry = ProjectRegistry()
        project = registry.open(str(self.root.resolve()))

        self.assertNotIn(str(self.root), project.project_id)
        self.assertGreaterEqual(len(project.project_id), 24)
        resolved = registry.resolve_file(project.project_id, "pkg/main.py")
        self.assertEqual(resolved.path, (self.root / "pkg" / "main.py").resolve())
        self.assertEqual(resolved.relative_path, "pkg/main.py")

    def test_rejects_absolute_parent_and_sibling_escape_paths(self) -> None:
        from app.projects import InvalidProjectPath, ProjectRegistry

        registry = ProjectRegistry()
        project = registry.open(str(self.root.resolve()))

        bad_paths = [
            str(self.outside.resolve()),
            "../outside.py",
            "pkg/../../outside.py",
            "C:\\Windows\\win.ini",
            "\\\\server\\share\\file.py",
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                with self.assertRaises(InvalidProjectPath):
                    registry.resolve_file(project.project_id, path)

    def test_unknown_project_id_is_not_a_path_fallback(self) -> None:
        from app.projects import ProjectNotFound, ProjectRegistry

        registry = ProjectRegistry()
        with self.assertRaises(ProjectNotFound):
            registry.resolve_file(str(self.root), "pkg/main.py")


class SecuritySettingsContractTest(unittest.TestCase):
    def test_production_only_binds_to_ipv4_loopback(self) -> None:
        from app.security import SecurityConfigurationError, SecuritySettings

        settings = SecuritySettings.production("127.0.0.1", 8710)
        self.assertEqual(settings.allowed_hosts, frozenset({"127.0.0.1:8710"}))
        self.assertEqual(settings.allowed_origins, frozenset({"http://127.0.0.1:8710"}))

        for host in ("0.0.0.0", "localhost", "::1", "192.168.1.10"):
            with self.subTest(host=host):
                with self.assertRaises(SecurityConfigurationError):
                    SecuritySettings.production(host, 8710)

    def test_each_default_production_session_has_a_random_secret(self) -> None:
        from app.security import SecuritySettings

        first = SecuritySettings.production("127.0.0.1", 8710)
        second = SecuritySettings.production("127.0.0.1", 8710)
        self.assertNotEqual(first.session_token, second.session_token)
        self.assertGreaterEqual(len(first.session_token), 32)

    def test_test_mode_must_be_explicitly_constructed(self) -> None:
        from app.security import SecuritySettings

        settings = SecuritySettings.testing(
            allowed_hosts={"testserver"},
            allowed_origins={"http://testserver"},
            session_token="test-session-token-at-least-32-bytes",
        )
        self.assertTrue(settings.is_test)
        self.assertIn("testserver", settings.allowed_hosts)


class PublicAPIContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(f"FastAPI test dependencies unavailable: {exc}")

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from app.main import create_app
        from app.security import SecuritySettings

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        self.outside = Path(self.tmp.name) / "outside.py"
        self.outside.write_text("secret\n", encoding="utf-8")
        self.token = "explicit-test-session-token-32-bytes!"
        settings = SecuritySettings.testing(
            allowed_hosts={"testserver"},
            allowed_origins={"http://testserver"},
            session_token=self.token,
        )
        self.client_context = TestClient(
            create_app(security_settings=settings, start_model=False))
        self.client = self.client_context.__enter__()
        self.headers = {"Origin": "http://testserver"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def authenticate(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.client.cookies.get("codereader_session"), self.token)

    def open_project(self) -> str:
        self.authenticate()
        response = self.client.post(
            "/api/projects/open", json={"path": str(self.root.resolve())}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn(str(self.root), str(body))
        return body["project_id"]

    def test_api_rejects_missing_cookie_bad_host_and_bad_origin(self) -> None:
        no_cookie = self.client.get("/api/health")
        self.assertEqual(no_cookie.status_code, 401)
        self.assertEqual(set(no_cookie.json()), {"error"})
        self.assertEqual(no_cookie.json()["error"]["code"], "invalid_session")

        self.authenticate()
        missing_origin = self.client.post(
            "/api/projects/open", json={"path": str(self.root.resolve())})
        self.assertEqual(missing_origin.status_code, 403)
        self.assertEqual(missing_origin.json()["error"]["code"], "origin_required")

        bad_origin = self.client.post(
            "/api/projects/open",
            json={"path": str(self.root.resolve())},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(bad_origin.status_code, 403)
        self.assertEqual(bad_origin.json()["error"]["code"], "invalid_origin")

        bad_host = self.client.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(bad_host.status_code, 403)
        self.assertEqual(bad_host.json()["error"]["code"], "invalid_host")

    def test_open_is_only_absolute_path_entry_and_file_is_project_relative(self) -> None:
        project_id = self.open_project()

        listing = self.client.get(f"/api/projects/{project_id}/browse")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["relative_path"], "")
        self.assertIn("main.py", {item["relative_path"] for item in listing.json()["entries"]})
        self.assertNotIn(str(self.root), str(listing.json()))

        response = self.client.get(f"/api/projects/{project_id}/files/main.py")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["path"], "main.py")

        encoded_absolute = str(self.outside.resolve()).replace("\\", "%5C").replace(":", "%3A")
        rejected = self.client.get(f"/api/projects/{project_id}/files/{encoded_absolute}")
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(rejected.json()["error"]["code"], "invalid_project_path")

        traversal = self.client.get(f"/api/projects/{project_id}/files/%2E%2E/outside.py")
        self.assertIn(traversal.status_code, (400, 404))
        if traversal.status_code == 400:
            self.assertEqual(traversal.json()["error"]["code"], "invalid_project_path")

    def test_validation_and_not_found_use_one_api_error_shape(self) -> None:
        self.authenticate()
        invalid = self.client.post("/api/projects/open", json={}, headers=self.headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(set(invalid.json()), {"error"})
        self.assertEqual(invalid.json()["error"]["code"], "validation_error")

        missing = self.client.get("/api/projects/not-a-capability/files/main.py")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "project_not_found")

    def test_sse_sequence_has_stable_envelope(self) -> None:
        from pydantic import ValidationError
        from app.schemas import StreamSequence

        stream = StreamSequence(job_id="job-new", scope_id="main.py")
        first = stream.event("status", {"state": "queued"}).model_dump()
        second = stream.event("delta", {"text": "a", "target": "overview"}).model_dump()
        self.assertEqual(first, {
            "job_id": "job-new", "seq": 0, "type": "status", "scope_id": "main.py",
            "payload": {"state": "queued"},
        })
        self.assertEqual(second["seq"], 1)
        self.assertEqual(second["job_id"], first["job_id"])

        compatible = stream.event("delta", {
            "text": "legacy", "target": "overview", "cached": True})
        self.assertTrue(compatible.payload["cached"])
        with self.assertRaises(ValidationError):
            stream.event("delta", {"text": "missing target"})

    def test_each_stream_request_gets_a_server_job_id_and_strict_payload(self) -> None:
        project_id = self.open_project()
        request_body = {
            "project_id": project_id,
            "relative_path": "main.py",
            "job_id": "reused-client-job",
        }

        job_ids = []
        for _ in range(2):
            with (
                patch("app.api.llm.health_check", new=AsyncMock(return_value=False)),
                patch("app.api.llama_launcher.ensure_running",
                      new=AsyncMock(return_value=False)),
            ):
                response = self.client.post(
                    "/api/explain", json=request_body, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(events[0]["payload"]["state"], "started")
            self.assertTrue({"code", "message"} <= set(events[-1]["payload"]))
            job_ids.append(events[0]["job_id"])

        self.assertNotIn("reused-client-job", job_ids)
        self.assertNotEqual(job_ids[0], job_ids[1])

    def test_model_switch_accepts_only_a_models_directory_basename(self) -> None:
        self.authenticate()
        models = Path(self.tmp.name) / "models"
        models.mkdir()
        outside_model = Path(self.tmp.name) / "outside.gguf"
        outside_model.write_bytes(b"not-a-model")

        with (
            patch("app.api.resolve_path", return_value=models),
            patch("app.api.update_config_file") as update,
            patch("app.api.llm.health_check", new=AsyncMock(return_value=False)),
        ):
            response = self.client.post(
                "/api/models/switch", json={"name": "../outside.gguf"},
                headers=self.headers)

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_model_name")
        update.assert_not_called()

    def test_failed_stream_does_not_write_partially_generated_cache(self) -> None:
        project_id = self.open_project()
        calls = 0

        async def failing_stream(*_, **__):
            nonlocal calls
            calls += 1
            yield "partial"
            if calls == 2:
                raise RuntimeError("synthetic segment failure")

        with (
            patch("app.api.llm.health_check", new=AsyncMock(return_value=True)),
            patch("app.api.llm.stream_chat", new=failing_stream),
            patch("app.api._retrieve_evidence", new=AsyncMock(return_value=[])),
            patch("app.api.cache.get", return_value=None),
            patch("app.api.cache.put") as cache_put,
        ):
            response = self.client.post(
                "/api/explain",
                json={"project_id": project_id, "relative_path": "main.py"},
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"type": "error"', response.text)
        cache_put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
