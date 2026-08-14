import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config


class ConfigHardeningTests(unittest.TestCase):
    def tearDown(self) -> None:
        config.reset_config_cache()

    def test_rejects_non_loopback_application_host(self) -> None:
        with mock.patch.object(config, "app_root", return_value=Path(tempfile.gettempdir())):
            with mock.patch.object(config, "_read_user_config", return_value={"app_host": "0.0.0.0"}):
                with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                    config.get_settings()

        with self.assertRaisesRegex(ValueError, "127.0.0.1"):
            config.validate_bind_host("0.0.0.0")

    def test_model_path_must_be_a_basename_under_models(self) -> None:
        bad_values = ("../outside.gguf", "C:/outside.gguf", "subdir/model.gguf")
        for value in bad_values:
            with self.subTest(value=value):
                raw = {"llama": {"model": value}}
                with mock.patch.object(config, "_read_user_config", return_value=raw):
                    with self.assertRaises(ValueError):
                        config.get_settings(force_reload=True)

    def test_update_is_atomic_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(config, "app_root", return_value=root):
                config.update_config_file(
                    lambda raw: raw.setdefault("llama", {}).update({"thinking": True})
                )
                loaded = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertTrue(loaded["llama"]["thinking"])
                self.assertFalse((root / "config.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
