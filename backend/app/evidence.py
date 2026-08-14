"""Validated source evidence shared by retrieval and context packing.

Evidence deliberately contains only project-relative paths and exact source spans.  A
consumer can therefore validate an item immediately before it is shown to a user or
sent to a model, instead of trusting stale index metadata.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union


def _decode_source(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Evidence:
    """A source-grounded result returned by the code retriever."""

    path: str
    start_line: int
    end_line: int
    content: str
    source_hash: str
    language: str
    relation: str
    symbol: Optional[str] = None
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "source_hash": self.source_hash,
            "language": self.language,
            "relation": self.relation,
            "symbol": self.symbol,
            "score": self.score,
            "metadata": dict(self.metadata),
        }

    def validate(self, root: Union[str, Path]) -> bool:
        """Verify containment, current content hash, line range and exact span text."""
        try:
            project_root = Path(root).resolve()
            candidate = (project_root / Path(self.path)).resolve()
            candidate.relative_to(project_root)
            if not candidate.is_file() or self.start_line < 1 or self.end_line < self.start_line:
                return False
            data = candidate.read_bytes()
            if hashlib.sha256(data).hexdigest() != self.source_hash:
                return False
            lines = _decode_source(data).splitlines()
            if self.end_line > len(lines):
                return False
            expected = "\n".join(lines[self.start_line - 1:self.end_line])
            return expected == self.content
        except (OSError, ValueError, RuntimeError):
            return False


__all__ = ["Evidence"]
