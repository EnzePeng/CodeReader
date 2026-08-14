"""A strictly bounded, read-only project exploration engine."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, List, Mapping, Sequence

from .evidence import Evidence


@dataclass(frozen=True)
class ExplorationRequest:
    tool: str
    arguments: Mapping[str, Any]


class ReadOnlyExplorer:
    """Execute at most three allowlisted retrieval operations.

    This is the code-enforced permission boundary for future model tool calls.
    It deliberately has no shell, process, network or filesystem-write handle.
    """

    ALLOWED_TOOLS = (
        "search_symbols",
        "find_definitions",
        "find_references",
        "search_text",
        "open_code_span",
    )

    def __init__(self, project_root: Path, retriever: Any, max_steps: int = 3) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.retriever = retriever
        self.max_steps = min(3, max(1, int(max_steps)))

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, Evidence):
            return value.to_dict()
        if isinstance(value, list):
            return [ReadOnlyExplorer._serialize(item) for item in value]
        return value

    @staticmethod
    def _bounded_text(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{name} is required")
        if len(text) > 500:
            raise ValueError(f"{name} is too long")
        return text

    def _path(self, raw: Any) -> Path:
        text = self._bounded_text(raw, "path")
        if PurePath(text).is_absolute():
            raise ValueError("path must stay inside the project")
        candidate = (self.project_root / text).resolve()
        try:
            candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise ValueError("path must stay inside the project") from exc
        if not candidate.is_file():
            raise ValueError("project file does not exist")
        return candidate

    def _open_code_span(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        path = self._path(arguments.get("path"))
        start = int(arguments.get("start_line", 1))
        end = int(arguments.get("end_line", start))
        if start < 1 or end < start or end - start > 400:
            raise ValueError("invalid code span")
        data = path.read_bytes()
        if len(data) > 2_000_000:
            raise ValueError("project file is too large")
        text = data.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        if end > len(lines):
            raise ValueError("code span exceeds file length")
        return {
            "path": path.relative_to(self.project_root).as_posix(),
            "start_line": start,
            "end_line": end,
            "content": "\n".join(lines[start - 1:end]),
        }

    def _invoke(self, request: ExplorationRequest) -> Any:
        if request.tool not in self.ALLOWED_TOOLS:
            raise ValueError(f"tool {request.tool!r} is not allowed")
        args = request.arguments
        if request.tool == "open_code_span":
            return self._open_code_span(args)
        query = self._bounded_text(args.get("query") or args.get("symbol"), "query")
        limit = min(20, max(1, int(args.get("limit", 8))))
        current_file = args.get("current_file")
        if current_file is not None:
            current_file = self._path(current_file).relative_to(self.project_root).as_posix()
        if request.tool == "search_symbols":
            return self.retriever.search_symbols(query, current_file=current_file, limit=limit)
        if request.tool == "find_definitions":
            return self.retriever.definitions(query, current_file=current_file, limit=limit)
        if request.tool == "find_references":
            return self.retriever.references(query, current_file=current_file, limit=limit)
        return self.retriever.search_text(query, limit=limit)

    def run(self, requests: Sequence[ExplorationRequest]) -> List[Any]:
        if len(requests) > self.max_steps:
            raise ValueError(f"exploration is limited to {self.max_steps} steps")
        return [self._serialize(self._invoke(request)) for request in requests]


__all__ = ["ExplorationRequest", "ReadOnlyExplorer"]
