"""Regression tests for warnings emitted while inspecting user Python source."""
import ast
import tempfile
import unittest
import warnings
from pathlib import Path

from app import project_index, segmenter
from app.code_index import CodeIndex


def _source_with_windows_path() -> str:
    # Build the invalid escape at runtime so this test module does not warn itself.
    return 'project_root = "D:' + "\\Repo\\Example" + '"\n'


class AstWarningIsolationTest(unittest.TestCase):
    def _syntax_warnings_from(self, action) -> list[warnings.WarningMessage]:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            action()
        return [item for item in captured if issubclass(item.category, SyntaxWarning)]

    def test_fixture_triggers_python_syntax_warning_without_isolation(self) -> None:
        captured = self._syntax_warnings_from(lambda: ast.parse(_source_with_windows_path()))
        self.assertTrue(captured)

    def test_segmenter_does_not_leak_indexed_source_warnings(self) -> None:
        captured = self._syntax_warnings_from(
            lambda: segmenter.segment_file(_source_with_windows_path(), ".py")
        )
        self.assertEqual([], captured)

    def test_persistent_index_does_not_leak_indexed_source_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            root.mkdir()
            (root / "paths.py").write_text(_source_with_windows_path(), encoding="utf-8")
            index = CodeIndex(base / "index.db")

            captured = self._syntax_warnings_from(lambda: index.index_project(root))

        self.assertEqual([], captured)

    def test_legacy_index_does_not_leak_indexed_source_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "paths.py").write_text(_source_with_windows_path(), encoding="utf-8")

            captured = self._syntax_warnings_from(
                lambda: project_index.build_index(str(root))
            )

        self.assertEqual([], captured)


if __name__ == "__main__":
    unittest.main()
