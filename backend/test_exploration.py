import os
import tempfile
import unittest
from pathlib import Path

from app.exploration import ExplorationRequest, ReadOnlyExplorer


class _FakeRetriever:
    class _Index:
        @staticmethod
        def project_revision(_root):
            return 7

        @staticmethod
        def file_record(_root, _path):
            return {"language": "python"}

    index = _Index()

    def definitions(self, symbol, current_file=None, limit=8):
        return [symbol]

    def references(self, symbol, current_file=None, limit=8):
        return [symbol]

    def search_symbols(self, query, current_file=None, limit=8):
        return [query]

    def search_text(self, query, limit=8):
        return [query]


class ReadOnlyExplorerTests(unittest.TestCase):
    def test_rejects_more_than_three_steps_and_unknown_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explorer = ReadOnlyExplorer(Path(tmp), _FakeRetriever())
            with self.assertRaisesRegex(ValueError, "3"):
                explorer.run([ExplorationRequest("search_text", {"query": "x"})] * 4)
            with self.assertRaisesRegex(ValueError, "not allowed"):
                explorer.run([ExplorationRequest("shell", {"query": "dir"})])

    def test_open_code_span_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            explorer = ReadOnlyExplorer(root, _FakeRetriever())

            result = explorer.run([ExplorationRequest(
                "open_code_span", {"path": "safe.py", "start_line": 2, "end_line": 3}
            )])

            self.assertEqual(result[0]["content"], "two\nthree")
            with self.assertRaisesRegex(ValueError, "project"):
                explorer.run([ExplorationRequest(
                    "open_code_span", {"path": "../outside.py", "start_line": 1, "end_line": 1}
                )])
            with self.assertRaisesRegex(ValueError, "project"):
                explorer.run([ExplorationRequest(
                    "open_code_span",
                    {"path": str((root / "safe.py").resolve()), "start_line": 1, "end_line": 1},
                )])
            with self.assertRaisesRegex(ValueError, "200"):
                explorer.run([ExplorationRequest(
                    "open_code_span", {"path": "safe.py", "start_line": 1, "end_line": 201}
                )])

            outside = root.parent / "linked-outside.py"
            outside.write_text("secret\n", encoding="utf-8")
            link = root / "link.py"
            try:
                os.symlink(outside, link)
            except OSError:
                pass  # Windows may require Developer Mode for symlink creation.
            else:
                with self.assertRaisesRegex(ValueError, "project"):
                    explorer.run([ExplorationRequest(
                        "open_code_span", {"path": "link.py", "start_line": 1, "end_line": 1}
                    )])

    def test_only_allowlisted_search_tools_are_exposed(self) -> None:
        self.assertEqual(
            set(ReadOnlyExplorer.ALLOWED_TOOLS),
            {
                "list_files", "search_symbols", "resolve_symbol", "find_references",
                "trace_calls", "search_text", "open_code_span",
            },
        )


if __name__ == "__main__":
    unittest.main()
