"""Strictly bounded, read-only repository tools for model-guided research."""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .evidence import Evidence

TOOL_SCHEMA_VERSION = "2"


@dataclass(frozen=True)
class ExplorationRequest:
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class StepContext:
    project_root: Path
    current_file: str
    index_revision: int
    model_config: tuple[tuple[str, str], ...] = ()
    tool_schema_version: str = TOOL_SCHEMA_VERSION


class ReadOnlyExplorer:
    """Code-enforced permission boundary with no shell, network, or write handle."""

    ALLOWED_TOOLS = (
        "list_files",
        "search_symbols",
        "resolve_symbol",
        "find_references",
        "trace_calls",
        "search_text",
        "open_code_span",
    )
    TOOL_DEFINITIONS: List[Dict[str, Any]] = [
        {"type": "function", "function": {"name": "list_files", "description": "List project-relative files matching an optional glob.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "cursor": {"type": "string"}}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "search_symbols", "description": "Search indexed symbol definitions.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}, "kind": {"type": "string"}, "cursor": {"type": "string"}}, "required": ["name"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "resolve_symbol", "description": "Resolve an expression or location to qualified symbol definitions.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "line": {"type": "integer", "minimum": 1}, "expression": {"type": "string"}, "cursor": {"type": "string"}}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "find_references", "description": "Find resolved references to a symbol id.", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "integer", "minimum": 1}, "cursor": {"type": "string"}}, "required": ["symbol_id"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "trace_calls", "description": "Trace one-hop callers or callees of a symbol id.", "parameters": {"type": "object", "properties": {"symbol_id": {"type": "integer", "minimum": 1}, "direction": {"type": "string", "enum": ["callers", "callees"]}, "depth": {"type": "integer", "const": 1}, "cursor": {"type": "string"}}, "required": ["symbol_id", "direction"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "search_text", "description": "Search source text with FTS/literal fallback.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "cursor": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "open_code_span", "description": "Read at most 200 lines and 64 KiB from a project file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "cursor": {"type": "string"}}, "required": ["path", "start_line", "end_line"], "additionalProperties": False}}},
    ]

    def __init__(
        self,
        project_root: Path,
        retriever: Any,
        max_steps: int = 3,
        current_file: str = "",
        model_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.retriever = retriever
        self.index = retriever.index
        self.max_steps = min(6, max(1, int(max_steps)))
        self.context = StepContext(
            self.project_root,
            current_file.replace("\\", "/"),
            int(self.index.project_revision(self.project_root)),
            tuple(sorted(
                (str(key), str(value)) for key, value in (model_config or {}).items()
            )),
        )

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, Evidence):
            return value.to_dict()
        if isinstance(value, list):
            return [ReadOnlyExplorer._serialize(item) for item in value]
        if isinstance(value, dict):
            return {key: ReadOnlyExplorer._serialize(item) for key, item in value.items()}
        return value

    @staticmethod
    def _bounded_text(value: Any, name: str, *, required: bool = True) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise ValueError(f"{name} is required")
        if len(text) > 500:
            raise ValueError(f"{name} is too long")
        return text

    def _relative_path(self, raw: Any, *, must_exist: bool = True) -> str:
        text = self._bounded_text(raw, "path")
        if PurePath(text).is_absolute():
            raise ValueError("path must stay inside the project")
        candidate = (self.project_root / text).resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("path must stay inside the project") from exc
        if must_exist and not candidate.is_file():
            raise ValueError("project file does not exist")
        return candidate.relative_to(self.project_root).as_posix()

    @staticmethod
    def _offset(arguments: Mapping[str, Any]) -> int:
        raw = arguments.get("cursor")
        if raw in (None, ""):
            return 0
        try:
            value = int(str(raw))
        except ValueError as exc:
            raise ValueError("cursor is invalid") from exc
        if value < 0 or value > 100_000:
            raise ValueError("cursor is invalid")
        return value

    @staticmethod
    def _refs(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        for item in items:
            path = item.get("path")
            start = item.get("start_line")
            end = item.get("end_line")
            if path and start and end:
                refs.append({
                    "path": path,
                    "start_line": start,
                    "end_line": end,
                    "source_hash": item.get("source_hash", ""),
                    "symbol": item.get("symbol"),
                })
        return refs

    def _page(self, values: Sequence[Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
        offset = self._offset(arguments)
        total = len(values)
        page = [self._serialize(item) for item in values[offset:offset + 30]]
        next_offset = offset + len(page)
        result = {
            "items": page,
            "evidence_refs": self._refs(item for item in page if isinstance(item, dict)),
            "total": total,
            "truncated": next_offset < total,
            "next_cursor": str(next_offset) if next_offset < total else None,
            "index_revision": self.context.index_revision,
            "tool_schema_version": self.context.tool_schema_version,
        }
        return result

    def _open_code_span(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        relative = self._relative_path(arguments.get("path"))
        path = (self.project_root / relative).resolve()
        start = int(arguments.get("start_line", 1))
        end = int(arguments.get("end_line", start))
        if start < 1 or end < start or end - start + 1 > 200:
            raise ValueError("invalid code span; maximum is 200 lines")
        data = path.read_bytes()
        if len(data) > 2_000_000:
            raise ValueError("project file is too large")
        text = data.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        if end > len(lines):
            raise ValueError("code span exceeds file length")
        content = "\n".join(lines[start - 1:end])
        encoded = content.encode("utf-8")
        if len(encoded) > 65_536:
            raise ValueError("code span exceeds 64 KiB")
        source_hash = hashlib.sha256(data).hexdigest()
        item = {
            "path": relative,
            "start_line": start,
            "end_line": end,
            "content": content,
            "source_hash": source_hash,
            "language": str((self.index.file_record(self.project_root, relative) or {}).get("language", "plaintext")),
            "relation": "definition",
            "symbol": None,
        }
        result = self._page([item], arguments)
        # Keep legacy top-level span fields while every new tool shares paging metadata.
        result.update(item)
        return result

    def _symbol_evidence(self, rows: Sequence[Dict[str, Any]], relation: str) -> List[Evidence]:
        output: List[Evidence] = []
        for rank, row in enumerate(rows, 1):
            item = self.retriever._from_symbol(row, relation, 1.0 - rank * 0.001, {
                "symbol_id": row.get("id"),
                "symbol_key": row.get("symbol_key"),
            })
            if item is not None:
                output.append(item)
        return output

    def invoke(self, request: ExplorationRequest) -> Dict[str, Any]:
        if request.tool not in self.ALLOWED_TOOLS:
            raise ValueError(f"tool {request.tool!r} is not allowed")
        args = request.arguments
        if request.tool == "open_code_span":
            return self._open_code_span(args)
        if request.tool == "list_files":
            pattern = self._bounded_text(args.get("pattern"), "pattern", required=False) or "*"
            file_values = [path for path in self.index.list_files(self.project_root)
                           if fnmatch.fnmatch(path, pattern)]
            return self._page(file_values, args)
        path_filter = args.get("path")
        normalized_path = self._relative_path(path_filter) if path_filter else ""
        if request.tool == "search_symbols":
            name = self._bounded_text(args.get("name"), "name")
            rows = self.index.search_symbol_rows(self.project_root, name, normalized_path or self.context.current_file, 200)
            kind = self._bounded_text(args.get("kind"), "kind", required=False)
            if normalized_path:
                rows = [row for row in rows if row["path"] == normalized_path]
            if kind:
                rows = [row for row in rows if row["kind"] == kind]
            return self._page(self._symbol_evidence(rows, "definition"), args)
        if request.tool == "resolve_symbol":
            expression = self._bounded_text(args.get("expression"), "expression", required=False)
            line = int(args["line"]) if args.get("line") is not None else None
            path_value = normalized_path or self.context.current_file
            rows = self.index.resolve_symbol_rows(self.project_root, path_value, line, expression, 100)
            return self._page(self._symbol_evidence(rows, "definition"), args)
        symbol_id = int(args.get("symbol_id", 0) or 0)
        if request.tool in {"find_references", "trace_calls"} and symbol_id < 1:
            raise ValueError("symbol_id is required")
        if request.tool == "find_references":
            rows = self.index.reference_rows(self.project_root, symbol_id, 200)
            reference_values: List[Evidence] = []
            for rank, row in enumerate(rows, 1):
                content = self.retriever._span(str(row["path"]), int(row["start_line"]), int(row["end_line"]))
                if content is not None:
                    reference_values.append(Evidence(
                        path=str(row["path"]), start_line=int(row["start_line"]),
                        end_line=int(row["end_line"]), content=content,
                        source_hash=str(row["source_hash"]), language=str(row["language"]),
                        relation="reference", symbol=None, score=0.95 - rank * 0.001,
                        metadata={"resolution_status": row.get("resolution_status")},
                    ))
            return self._page(reference_values, args)
        if request.tool == "trace_calls":
            direction = self._bounded_text(args.get("direction"), "direction")
            if direction not in {"callers", "callees"} or int(args.get("depth", 1)) != 1:
                raise ValueError("trace_calls only supports callers/callees at depth 1")
            rows = self.index.trace_rows(self.project_root, symbol_id, direction, 200)
            return self._page(self._symbol_evidence(rows, direction[:-1]), args)
        query = self._bounded_text(args.get("query"), "query")
        values = self.retriever.search_text(query, limit=200)
        if normalized_path:
            values = [item for item in values if item.path == normalized_path]
        return self._page(values, args)

    def run(self, requests: Sequence[ExplorationRequest]) -> List[Dict[str, Any]]:
        if len(requests) > self.max_steps:
            raise ValueError(f"exploration is limited to {self.max_steps} steps")
        return [self.invoke(request) for request in requests]


__all__ = [
    "ExplorationRequest", "ReadOnlyExplorer", "StepContext", "TOOL_SCHEMA_VERSION",
]
