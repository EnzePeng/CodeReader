import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.code_index import SCHEMA_VERSION, CodeIndex


class CrossFileSemanticIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "pkg").mkdir()
        (self.root / "a.py").write_text(
            "def foo():\n    return 'a'\n\nclass Repo:\n    def work(self):\n        return 'a'\n",
            encoding="utf-8",
        )
        (self.root / "z.py").write_text(
            "def foo():\n    return 'z'\n\nclass Repo:\n    def work(self):\n        return 'z'\n",
            encoding="utf-8",
        )
        (self.root / "consumer.py").write_text(
            "from z import foo as imported_foo, Repo\n"
            "import z as zmod\n\n"
            "def run(unknown):\n"
            "    imported_foo()\n"
            "    zmod.foo()\n"
            "    repo = Repo()\n"
            "    repo.work()\n"
            "    unknown.work()\n",
            encoding="utf-8",
        )
        (self.root / "inheritance.py").write_text(
            "class Base:\n"
            "    def work(self):\n        return 1\n\n"
            "class Child(Base):\n"
            "    def via_self(self):\n        return self.work()\n"
            "    def via_super(self):\n        return super().work()\n\n"
            "class Holder:\n"
            "    def __init__(self):\n        self.repo = Child()\n"
            "    def execute(self):\n        return self.repo.work()\n",
            encoding="utf-8",
        )
        (self.root / "nested.py").write_text(
            "def outer():\n"
            "    def inner():\n        return 1\n"
            "    return inner()\n",
            encoding="utf-8",
        )
        (self.root / "pkg" / "__init__.py").write_text(
            "from .impl import exported\n", encoding="utf-8")
        (self.root / "pkg" / "impl.py").write_text(
            "def exported():\n    return 42\n", encoding="utf-8")
        (self.root / "pkg" / "use.py").write_text(
            "from . import exported\n\ndef use():\n    return exported()\n", encoding="utf-8")
        (self.root / "cycle_a.py").write_text(
            "from cycle_b import b\n\ndef a():\n    return b()\n", encoding="utf-8")
        (self.root / "cycle_b.py").write_text(
            "from cycle_a import a\n\ndef b():\n    return 1\n", encoding="utf-8")
        self.db = base / "index.db"
        self.index = CodeIndex(self.db)
        self.index.index_project(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _call(self, path: str, line: int, name: str):
        rows = self.index.call_rows(self.root, name, limit=200)
        return next(row for row in rows if row["path"] == path and row["start_line"] == line)

    def _target(self, path: str, line: int, name: str):
        call = self._call(path, line, name)
        self.assertEqual(call["resolution_status"], "resolved")
        return self.index.symbol_by_id(self.root, int(call["target_symbol_id"]))

    def test_explicit_alias_relative_reexport_and_cycles_resolve_exactly(self) -> None:
        self.assertEqual(self._target("consumer.py", 5, "foo")["path"], "z.py")
        self.assertEqual(self._target("consumer.py", 6, "foo")["path"], "z.py")
        exported = self._target("pkg/use.py", 4, "exported")
        self.assertEqual((exported["path"], exported["qualified_name"]), ("pkg/impl.py", "exported"))
        self.assertEqual(self._target("cycle_a.py", 4, "b")["path"], "cycle_b.py")

    def test_instances_self_super_inheritance_and_nested_scope(self) -> None:
        repo_work = self._target("consumer.py", 8, "work")
        self.assertEqual((repo_work["path"], repo_work["qualified_name"]), ("z.py", "Repo.work"))
        for line in (7, 9, 15):
            target = self._target("inheritance.py", line, "work")
            self.assertEqual(target["qualified_name"], "Base.work")
        nested = self._target("nested.py", 4, "inner")
        self.assertEqual(nested["qualified_name"], "outer.inner")

    def test_unknown_receiver_with_same_name_is_ambiguous_not_false_exact(self) -> None:
        call = self._call("consumer.py", 9, "work")
        self.assertEqual(call["resolution_status"], "ambiguous")
        self.assertIsNone(call["target_symbol_id"])

    def test_mixed_natural_language_query_still_recalls_identifier(self) -> None:
        rows = self.index.search_text(self.root, "foo 这个函数内部怎么实现", limit=20)
        self.assertTrue(any("def foo" in row["content"] for row in rows))

    def test_incompatible_schema_is_built_then_atomically_replaced(self) -> None:
        old = Path(self.temp.name) / "old.db"
        conn = sqlite3.connect(old)
        try:
            conn.executescript(
                "CREATE TABLE schema_version(version INTEGER NOT NULL);"
                "INSERT INTO schema_version VALUES (1);"
                "CREATE TABLE legacy_marker(value TEXT);"
            )
            conn.commit()
        finally:
            conn.close()
        CodeIndex(old)
        conn = sqlite3.connect(old)
        try:
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            marker = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_marker'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIsNone(marker)


if __name__ == "__main__":
    unittest.main()
