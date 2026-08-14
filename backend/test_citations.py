import unittest

from app.citations import CitationFilter, EvidenceCatalog


class CitationTests(unittest.TestCase):
    def test_catalog_assigns_stable_ids_and_deduplicates_spans(self) -> None:
        catalog = EvidenceCatalog()
        first = catalog.add([{
            "path": "a.py", "start_line": 1, "end_line": 2,
            "content": "def a():\n    pass", "relation": "definition",
            "source_hash": "hash", "score": 1.0,
        }])[0]
        repeated = catalog.add([{
            "path": "a.py", "start_line": 1, "end_line": 2,
            "content": "def a():\n    pass", "relation": "definition",
            "source_hash": "hash", "score": 0.5,
        }])[0]

        self.assertEqual(first["id"], "E1")
        self.assertEqual(repeated["id"], "E1")
        self.assertIn("[E1] a.py:1-2", catalog.prompt_text([first]))

    def test_stream_filter_removes_invalid_citations_across_chunks(self) -> None:
        citation_filter = CitationFilter({"E1"})
        chunks = ["依据 [E", "1] 可知，另见 [E", "9]。"]
        output = "".join(citation_filter.feed(chunk) for chunk in chunks)
        output += citation_filter.flush()

        self.assertEqual(output, "依据 [E1] 可知，另见 。")
        self.assertEqual(citation_filter.invalid_ids, {"E9"})


if __name__ == "__main__":
    unittest.main()
