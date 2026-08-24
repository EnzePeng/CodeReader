"""Hybrid code retrieval over exact Python facts, call edges and SQLite FTS5."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .code_index import CodeIndex
from .evidence import Evidence

_RELATION_ORDER = {
    "definition": 0,
    "reference": 1,
    "caller": 2,
    "callee": 3,
    "text": 4,
}


class Retriever:
    """Retrieve validated, line-addressable source evidence from a ``CodeIndex``."""

    def __init__(self, index: CodeIndex, root: Union[str, Path]) -> None:
        self.index = index
        self.root = Path(root).resolve()

    def _span(self, path: str, start_line: int, end_line: int) -> Optional[str]:
        try:
            candidate = (self.root / path).resolve()
            candidate.relative_to(self.root)
            data = candidate.read_bytes()
            for encoding in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    text = data.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()
            if start_line < 1 or end_line < start_line or end_line > len(lines):
                return None
            return "\n".join(lines[start_line - 1:end_line])
        except (OSError, ValueError):
            return None

    def _from_symbol(self, row: Dict[str, Any], relation: str,
                     score: float, metadata: Optional[Dict[str, Any]] = None) -> Optional[Evidence]:
        start, end = int(row["start_line"]), int(row["end_line"])
        content = self._span(str(row["path"]), start, end)
        if content is None:
            return None
        return Evidence(
            path=str(row["path"]), start_line=start, end_line=end, content=content,
            source_hash=str(row["source_hash"]), language=str(row["language"]),
            relation=relation, symbol=str(row.get("name") or "") or None,
            score=score,
            metadata={
                "kind": row.get("kind", "symbol"),
                "qualified_name": row.get("qualified_name", row.get("name", "")),
                **(metadata or {}),
            },
        )

    def definitions(self, symbol: str, current_file: Optional[str] = None,
                    limit: int = 20) -> List[Evidence]:
        out: List[Evidence] = []
        for rank, row in enumerate(self.index.symbol_rows(self.root, symbol, current_file, limit), 1):
            item = self._from_symbol(row, "definition", 1.0 - min(rank - 1, 20) * 0.01,
                                     {"rank": rank})
            if item is not None:
                out.append(item)
        return out

    def references(self, symbol: str, current_file: Optional[str] = None,
                   limit: int = 50) -> List[Evidence]:
        rows = self.index.call_rows(self.root, symbol, limit=max(limit * 2, limit))
        if current_file:
            normalized = current_file.replace("\\", "/").casefold()
            rows.sort(key=lambda row: (
                0 if str(row["path"]).replace("\\", "/").casefold() == normalized else 1,
                str(row["path"]).casefold(), int(row["start_line"]),
            ))
        out: List[Evidence] = []
        for rank, row in enumerate(rows[:limit], 1):
            start, end = int(row["start_line"]), int(row["end_line"])
            content = self._span(str(row["path"]), start, end)
            if content is None:
                continue
            out.append(Evidence(
                path=str(row["path"]), start_line=start, end_line=end, content=content,
                source_hash=str(row["source_hash"]), language=str(row["language"]),
                relation="reference", symbol=str(row.get("target_name") or row["callee_name"]),
                score=0.95 - min(rank - 1, 20) * 0.01,
                metadata={
                    "rank": rank,
                    "caller": row.get("caller_name") or None,
                    "receiver": row.get("receiver") or "",
                    "call_id": row.get("id"),
                    "resolution_status": row.get("resolution_status"),
                    "confidence": row.get("confidence"),
                },
            ))
        return out

    def _graph(self, symbol: str, relation: str, limit: int) -> List[Evidence]:
        rows = (self.index.caller_rows(self.root, symbol, limit)
                if relation == "caller" else self.index.callee_rows(self.root, symbol, limit))
        out: List[Evidence] = []
        for rank, row in enumerate(rows, 1):
            item = self._from_symbol(
                row, relation, 0.88 - min(rank - 1, 20) * 0.01,
                {"rank": rank, "graph_origin": symbol, "symbol_id": row.get("id")},
            )
            if item is not None:
                out.append(item)
        return out

    def search_symbols(self, query: str, current_file: Optional[str] = None,
                       limit: int = 20) -> List[Evidence]:
        out: List[Evidence] = []
        for rank, row in enumerate(
            self.index.search_symbol_rows(self.root, query, current_file, limit), 1
        ):
            exact = str(row["name"]).casefold() == query.strip().casefold()
            item = self._from_symbol(row, "definition", (1.0 if exact else 0.8) - rank * 0.001,
                                     {"rank": rank, "match": "exact" if exact else "fuzzy"})
            if item is not None:
                out.append(item)
        return out

    def search_files(self, query: str, limit: int = 20) -> List[Evidence]:
        out: List[Evidence] = []
        for rank, row in enumerate(self.index.search_file_rows(self.root, query, limit), 1):
            line_count = int(row.get("line_count") or 0)
            if line_count <= 0:
                continue
            end = min(line_count, 40)
            content = self._span(str(row["path"]), 1, end)
            if content is None:
                continue
            out.append(Evidence(
                path=str(row["path"]), start_line=1, end_line=end, content=content,
                source_hash=str(row["source_hash"]), language=str(row["language"]),
                relation="text", symbol=None, score=0.7 - rank * 0.001,
                metadata={"rank": rank, "match": "file_path"},
            ))
        return out

    def search_text(self, query: str, limit: int = 30) -> List[Evidence]:
        out: List[Evidence] = []
        for rank, row in enumerate(self.index.search_text(self.root, query, limit), 1):
            bm25 = float(row.get("bm25_score") or 0.0)
            content = str(row["content"])
            out.append(Evidence(
                path=str(row["path"]), start_line=int(row["start_line"]),
                end_line=int(row["end_line"]), content=content,
                source_hash=str(row["source_hash"]), language=str(row["language"]),
                relation="text", symbol=None,
                score=1.0 / (1.0 + abs(bm25)),
                metadata={"rank": rank, "bm25": bm25, "chunk_id": row.get("id")},
            ))
        return out

    @staticmethod
    def _symbol_candidates(query: str) -> List[str]:
        values: List[str] = []
        stripped = query.strip()
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", stripped):
            values.append(stripped.rsplit(".", 1)[-1])
        for token in re.findall(r"[A-Za-z_]\w*", query):
            if token not in values:
                values.append(token)
        return values[:12]

    def retrieve(self, query: str, current_file: Optional[str] = None,
                 limit: int = 12) -> List[Evidence]:
        """Hybrid retrieval with structured facts ordered ahead of lexical chunks.

        Reciprocal-rank scores are retained as metadata for explainability.  Relation
        tiers are an intentional guardrail: a lexical mention never outranks a verified
        definition, reference, caller or callee solely because BM25 is confident.
        """
        definitions: List[Evidence] = []
        references: List[Evidence] = []
        graph: List[Evidence] = []
        matched_symbols: List[str] = []
        for candidate in self._symbol_candidates(query):
            found = self.definitions(candidate, current_file, limit=5)
            if not found:
                continue
            definitions.extend(found)
            matched_symbols.append(candidate)
            references.extend(self.references(candidate, current_file, limit=10))
            graph.extend(self._graph(candidate, "caller", 6))
            graph.extend(self._graph(candidate, "callee", 6))
        lexical = self.search_text(query, limit=max(limit * 3, 20))

        ranked_lists: Sequence[Sequence[Evidence]] = (
            definitions, references, graph, lexical,
        )
        by_key: Dict[Tuple[Any, ...], Evidence] = {}
        rrf: Dict[Tuple[Any, ...], float] = {}
        source_names = ("definitions", "references", "graph", "fts5")
        sources: Dict[Tuple[Any, ...], List[str]] = {}
        for source_name, items in zip(source_names, ranked_lists):
            for rank, item in enumerate(items, 1):
                key = (item.path, item.start_line, item.end_line, item.relation, item.symbol)
                by_key.setdefault(key, item)
                rrf[key] = rrf.get(key, 0.0) + 1.0 / (60.0 + rank)
                sources.setdefault(key, []).append(source_name)

        merged: List[Evidence] = []
        for key, item in by_key.items():
            metadata = dict(item.metadata)
            metadata.update({
                "rrf_score": rrf[key],
                "retrieval_sources": sources[key],
                "matched_symbols": matched_symbols,
            })
            merged.append(replace(item, metadata=metadata))
        merged.sort(key=lambda item: (
            _RELATION_ORDER.get(item.relation, 99),
            -float(item.metadata.get("rrf_score", 0.0)),
            -float(item.score),
            item.path.casefold(),
            item.start_line,
        ))
        return merged[:max(0, limit)]


__all__ = ["Retriever"]
