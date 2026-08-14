import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import cache, explainer


class CacheIntegrityTests(unittest.TestCase):
    def test_complete_prompt_digest_separates_same_code_in_different_files(self) -> None:
        messages = [{"role": "user", "content": "context"}]
        first = explainer.request_cache_key(
            kind="segment", relative_path="a.py", messages=messages,
            mode="simple", evidence_signature="ev-a"
        )
        second = explainer.request_cache_key(
            kind="segment", relative_path="b.py", messages=messages,
            mode="simple", evidence_signature="ev-a"
        )
        self.assertNotEqual(first, second)

    def test_empty_content_is_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache.close()
            with mock.patch.object(cache, "data_dir", return_value=Path(tmp)):
                cache.put("empty", "a.py", "segment", "  ", "model")
                self.assertIsNone(cache.get("empty"))
            cache.close()


if __name__ == "__main__":
    unittest.main()
