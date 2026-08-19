import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


class CodeIndexEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.db = self.base / "index.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fixture(self) -> None:
        (self.root / ".gitignore").write_text(
            "ignored.py\nignored_dir/*\n!ignored_dir/keep.md\n", encoding="utf-8"
        )
        (self.root / ".codereaderignore").write_text("private.sql\n", encoding="utf-8")
        (self.root / "README.md").write_text("Project architecture needle-readme\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("fastapi>=0.110\n", encoding="utf-8")
        (self.root / "config.json").write_text('{"feature": "needle-config"}\n', encoding="utf-8")
        (self.root / "schema.sql").write_text("CREATE TABLE audit_log(id INTEGER);\n", encoding="utf-8")
        (self.root / "test_app.py").write_text(
            "def test_visible():\n    assert True\n", encoding="utf-8"
        )
        (self.root / "app.py").write_text(
            "def helper(value: int) -> int:\n"
            "    return value + 1\n\n"
            "async def modern_runner(items):\n"
            "    return [helper(item) for item in items]\n",
            encoding="utf-8",
        )
        # Parse failures must remain searchable instead of disappearing from the index.
        (self.root / "broken.py").write_text(
            "def broken(:\n    needle_broken = 42\n", encoding="utf-8"
        )
        (self.root / "ignored.py").write_text("ignored_secret = True\n", encoding="utf-8")
        (self.root / "private.sql").write_text("SELECT private_secret;\n", encoding="utf-8")
        ignored_dir = self.root / "ignored_dir"
        ignored_dir.mkdir()
        (ignored_dir / "drop.md").write_text("drop me\n", encoding="utf-8")
        (ignored_dir / "keep.md").write_text("negation keeps this needle-keep\n", encoding="utf-8")

    def test_schema_coverage_ignore_rules_and_parse_error_search(self) -> None:
        from app.code_index import SCHEMA_VERSION, CodeIndex

        self._write_fixture()
        index = CodeIndex(self.db)
        status = index.index_project(self.root)

        self.assertEqual(SCHEMA_VERSION, status.schema_version)
        self.assertEqual(status.root, str(self.root.resolve()))
        self.assertIn("broken.py", status.parse_errors)

        paths = set(index.list_files(self.root))
        expected = {
            "README.md", "requirements.txt", "config.json", "schema.sql",
            "test_app.py", "app.py", "broken.py", "ignored_dir/keep.md",
        }
        self.assertTrue(expected.issubset(paths), paths)
        self.assertNotIn("ignored.py", paths)
        self.assertNotIn("private.sql", paths)
        self.assertNotIn("ignored_dir/drop.md", paths)

        hits = index.search_text(self.root, "needle_broken", limit=10)
        self.assertTrue(any(hit["path"] == "broken.py" for hit in hits), hits)

        conn = sqlite3.connect(self.db)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )}
            for table in (
                "schema_version", "projects", "files", "symbols", "calls",
                "text_chunks", "text_chunks_fts",
            ):
                self.assertIn(table, tables)
        finally:
            conn.close()

    def test_mtime_size_screen_hash_confirmation_update_and_delete(self) -> None:
        from app.code_index import CodeIndex

        source = self.root / "module.py"
        source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        index = CodeIndex(self.db)

        first = index.index_project(self.root)
        before = index.file_record(self.root, "module.py")
        self.assertEqual(1, first.indexed_files)
        self.assertEqual(1, first.added_files)

        second = index.index_project(self.root)
        same = index.file_record(self.root, "module.py")
        self.assertEqual(1, second.reused_files)
        self.assertEqual(before["indexed_at"], same["indexed_at"])

        # Metadata changed but bytes did not: hash confirmation should avoid re-parsing.
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        touched = index.index_project(self.root)
        after_touch = index.file_record(self.root, "module.py")
        self.assertEqual(1, touched.hash_confirmed_files)
        self.assertEqual(0, touched.updated_files)
        self.assertEqual(before["source_hash"], after_touch["source_hash"])
        self.assertEqual(before["indexed_at"], after_touch["indexed_at"])

        # Same byte length, different content: metadata screen triggers hash and re-index.
        source.write_text("def bravo():\n    return 2\n", encoding="utf-8")
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        changed = index.index_project(self.root)
        self.assertEqual(1, changed.updated_files)
        names = {row["name"] for row in index.symbol_rows(self.root, "bravo")}
        self.assertEqual({"bravo"}, names)
        self.assertFalse(index.symbol_rows(self.root, "alpha"))

        source.unlink()
        removed = index.index_project(self.root)
        self.assertEqual(1, removed.removed_files)
        self.assertNotIn("module.py", index.list_files(self.root))

    def test_evidence_checks_source_hash_and_line_bounds(self) -> None:
        from app.code_index import CodeIndex
        from app.retriever import Retriever

        source = self.root / "module.py"
        source.write_text("def helper():\n    return 1\n", encoding="utf-8")
        index = CodeIndex(self.db)
        index.index_project(self.root)
        evidence = Retriever(index, self.root).definitions("helper")[0]

        self.assertTrue(evidence.validate(self.root))
        self.assertFalse(replace(evidence, end_line=999).validate(self.root))
        payload = evidence.to_dict()
        self.assertEqual("module.py", payload["path"])
        self.assertRegex(payload["source_hash"], r"^[0-9a-f]{64}$")

        source.write_text("def helper():\n    return 2\n", encoding="utf-8")
        self.assertFalse(evidence.validate(self.root))


if __name__ == "__main__":
    unittest.main()
