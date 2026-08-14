import tempfile
import unittest
from pathlib import Path


class RetrieverEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.db = self.base / "index.db"
        (self.root / "a.py").write_text(
            "def helper(value):\n"
            "    return value + 1\n\n"
            "def entry():\n"
            "    return helper(1)\n",
            encoding="utf-8",
        )
        (self.root / "b.py").write_text(
            "from a import helper as h\n\n"
            "def caller():\n"
            "    return h(2)\n",
            encoding="utf-8",
        )
        (self.root / "README.md").write_text(
            "The helper guide also explains database migration planning.\n", encoding="utf-8"
        )
        (self.root / "migration.sql").write_text(
            "-- database migration\nCREATE TABLE accounts(id INTEGER);\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _retriever(self):
        from app.code_index import CodeIndex
        from app.retriever import Retriever

        index = CodeIndex(self.db)
        index.index_project(self.root)
        return index, Retriever(index, self.root)

    def test_exact_definition_references_and_alias_calls_precede_fts(self) -> None:
        index, retriever = self._retriever()

        definitions = retriever.definitions("helper")
        self.assertEqual("definition", definitions[0].relation)
        self.assertEqual(("a.py", 1), (definitions[0].path, definitions[0].start_line))

        refs = retriever.references("helper")
        locations = {(item.path, item.start_line) for item in refs}
        self.assertIn(("a.py", 5), locations)
        self.assertIn(("b.py", 4), locations)

        rows = index.call_rows(self.root, "helper")
        self.assertEqual({"a.py", "b.py"}, {row["path"] for row in rows})
        self.assertTrue(all(row["target_symbol_id"] is not None for row in rows), rows)

        result = retriever.retrieve("helper", current_file="b.py", limit=10)
        self.assertEqual("definition", result[0].relation)
        self.assertTrue(any(item.relation == "reference" for item in result), result)
        first_text = next((i for i, item in enumerate(result) if item.relation == "text"), 999)
        last_structured = max(
            i for i, item in enumerate(result)
            if item.relation in {"definition", "reference", "caller", "callee"}
        )
        self.assertGreater(first_text, last_structured)

    def test_fts_and_rrf_find_docs_and_sql_without_exact_symbol(self) -> None:
        _, retriever = self._retriever()
        result = retriever.retrieve("database migration", limit=10)
        paths = {item.path for item in result}
        self.assertIn("migration.sql", paths)
        self.assertIn("README.md", paths)
        self.assertTrue(all(item.validate(self.root) for item in result))
        self.assertTrue(all("rrf_score" in item.metadata for item in result))

    def test_symbol_search_and_direct_call_graph_expansion(self) -> None:
        _, retriever = self._retriever()
        symbols = retriever.search_symbols("help")
        self.assertEqual("helper", symbols[0].symbol)

        result = retriever.retrieve("entry", limit=10)
        self.assertEqual("definition", result[0].relation)
        self.assertTrue(
            any(item.relation == "callee" and item.symbol == "helper" for item in result),
            result,
        )

    def test_nested_caller_is_attributed_to_innermost_function(self) -> None:
        (self.root / "nested.py").write_text(
            "def helper2():\n"
            "    return 2\n\n"
            "def outer():\n"
            "    def inner():\n"
            "        return helper2()\n"
            "    return inner()\n",
            encoding="utf-8",
        )
        _, retriever = self._retriever()
        callers = [item for item in retriever.retrieve("helper2", limit=10)
                   if item.relation == "caller"]
        self.assertTrue(any(item.symbol == "inner" for item in callers), callers)


if __name__ == "__main__":
    unittest.main()
