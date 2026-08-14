"""Stable evidence IDs and streaming citation validation."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

_COMPLETE_CITATION = re.compile(r"^\[E(\d+)\]")
_POSSIBLE_PREFIX = re.compile(r"^\[?(?:E(?:\d+)?)?$", re.IGNORECASE)


class EvidenceCatalog:
    def __init__(self) -> None:
        self._ids: Dict[Tuple[Any, ...], str] = {}

    @staticmethod
    def _key(item: Mapping[str, Any]) -> Tuple[Any, ...]:
        return (
            str(item.get("path", "")).replace("\\", "/"),
            int(item.get("start_line", 0)),
            int(item.get("end_line", 0)),
            str(item.get("source_hash", "")),
            str(item.get("relation", "")),
            str(item.get("symbol", "")),
        )

    def add(self, items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for raw in items:
            item = dict(raw)
            key = self._key(item)
            evidence_id = self._ids.get(key)
            if evidence_id is None:
                evidence_id = f"E{len(self._ids) + 1}"
                self._ids[key] = evidence_id
            item["id"] = evidence_id
            result.append(item)
        return result

    @property
    def valid_ids(self) -> Set[str]:
        return set(self._ids.values())

    @staticmethod
    def prompt_text(items: Sequence[Mapping[str, Any]]) -> str:
        blocks: List[str] = []
        for item in items:
            symbol = f" · {item['symbol']}" if item.get("symbol") else ""
            blocks.append(
                f"[{item['id']}] {item['path']}:{item['start_line']}-{item['end_line']}"
                f" · {item.get('relation', 'text')}{symbol}\n{item.get('content', '')}"
            )
        if not blocks:
            return ""
        return (
            "## 可验证代码证据\n"
            "只可引用下列证据 ID；代码事实后用 [E1] 形式标注。\n\n"
            + "\n\n".join(blocks)
        )


class CitationFilter:
    """Remove citation IDs absent from the validated catalog while streaming."""

    def __init__(self, valid_ids: Set[str]) -> None:
        self.valid_ids = set(valid_ids)
        self.invalid_ids: Set[str] = set()
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        output: List[str] = []
        while self._buffer:
            bracket = self._buffer.find("[")
            if bracket < 0:
                output.append(self._buffer)
                self._buffer = ""
                break
            if bracket:
                output.append(self._buffer[:bracket])
                self._buffer = self._buffer[bracket:]
            match = _COMPLETE_CITATION.match(self._buffer)
            if match:
                raw = match.group(0)
                evidence_id = "E" + match.group(1)
                if evidence_id in self.valid_ids:
                    output.append(raw)
                else:
                    self.invalid_ids.add(evidence_id)
                self._buffer = self._buffer[len(raw):]
                continue
            closing = self._buffer.find("]")
            if closing >= 0 or len(self._buffer) > 20:
                output.append("[")
                self._buffer = self._buffer[1:]
                continue
            if _POSSIBLE_PREFIX.match(self._buffer):
                break
            output.append("[")
            self._buffer = self._buffer[1:]
        return "".join(output)

    def flush(self) -> str:
        remaining, self._buffer = self._buffer, ""
        return remaining


__all__ = ["CitationFilter", "EvidenceCatalog"]
