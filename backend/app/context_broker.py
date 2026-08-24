"""Deterministic, evidence-first context orchestration for code questions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .code_index import CodeIndex
from .conversation import EvidenceAnchor
from .evidence import Evidence
from .retriever import Retriever

_IMPLEMENTATION_TERMS = re.compile(
    r"实现|内部|源码|逻辑|怎么做|如何做|implement|implementation|definition|source",
    re.IGNORECASE,
)
_RELATION_TERMS = re.compile(
    r"调用|引用|影响|谁用|调用链|caller|callee|reference|impact|used by",
    re.IGNORECASE,
)
_MULTI_HOP_TERMS = re.compile(
    r"(?:两|三|[2-9])\s*跳|多跳|完整调用链|最终调用|最终读取|"
    r"two[- ]hop|multi[- ]hop|transitive|depth\s*[2-9]",
    re.IGNORECASE,
)
_CALLER_FOCUS_TERMS = re.compile(
    r"谁(?:在)?调用|哪里调用|哪些.*调用|被.*调用|调用链|引用|影响|"
    r"caller|reference|impact|used by",
    re.IGNORECASE,
)
_STOP_IDENTIFIERS = {
    "a", "an", "and", "are", "code", "does", "file", "for", "from", "function",
    "how", "in", "inside", "is", "it", "of", "or", "please", "the", "this", "to",
    "what", "where", "which", "why",
}


@dataclass(frozen=True)
class ContextResult:
    evidence: List[Evidence]
    repository_map: str
    sufficient: bool
    reason: str
    target_symbol_ids: List[int] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    index_revision: int = 0
    direct_target: bool = False
    multi_hop: bool = False


class ContextBroker:
    """Prefetch exact source facts and decide whether bounded exploration is needed."""

    def __init__(
        self,
        index: CodeIndex,
        retriever: Retriever,
        root: Path,
        repo_map_tokens: int = 900,
    ) -> None:
        self.index = index
        self.retriever = retriever
        self.root = root.resolve(strict=True)
        self.repo_map_tokens = max(128, int(repo_map_tokens))

    @staticmethod
    def _identifiers(question: str) -> List[str]:
        output: List[str] = []
        for value in re.findall(r"[A-Za-z_]\w*", question):
            if value.casefold() in _STOP_IDENTIFIERS or value in output:
                continue
            output.append(value)
        return output[:16]

    def _span(self, path: str, start: int, end: int) -> Optional[str]:
        try:
            candidate = (self.root / path).resolve()
            candidate.relative_to(self.root)
            lines = candidate.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if start < 1 or end < start or end > len(lines):
                return None
            return "\n".join(lines[start - 1:end])
        except (OSError, ValueError):
            return None

    def _from_symbol(
        self,
        row: Dict[str, Any],
        *,
        score: float,
        resolution: str,
    ) -> Optional[Evidence]:
        start, end = int(row["start_line"]), int(row["end_line"])
        content = self._span(str(row["path"]), start, end)
        if content is None:
            return None
        return Evidence(
            path=str(row["path"]), start_line=start, end_line=end, content=content,
            source_hash=str(row["source_hash"]), language=str(row["language"]),
            relation="definition", symbol=str(row.get("name") or "") or None,
            score=score,
            metadata={
                "symbol_id": int(row["id"]),
                "symbol_key": row.get("symbol_key"),
                "qualified_name": row.get("qualified_name"),
                "module_name": row.get("module_name"),
                "resolution": resolution,
            },
        )

    def _selection(self, path: str, start: int, end: int) -> Optional[Evidence]:
        record = self.index.file_record(self.root, path)
        content = self._span(path, start, end)
        if record is None or content is None:
            return None
        return Evidence(
            path=path, start_line=start, end_line=end, content=content,
            source_hash=str(record["source_hash"]), language=str(record["language"]),
            relation="definition", symbol=None, score=1.1,
            metadata={"selection": True, "resolution": "exact"},
        )

    def _anchor(self, anchor: EvidenceAnchor) -> Optional[Evidence]:
        record = self.index.file_record(self.root, anchor.path)
        if record is None or (anchor.source_hash and anchor.source_hash != record["source_hash"]):
            return None
        content = self._span(anchor.path, anchor.start_line, anchor.end_line)
        if content is None:
            return None
        item = Evidence(
            path=anchor.path, start_line=anchor.start_line, end_line=anchor.end_line,
            content=content, source_hash=str(record["source_hash"]),
            language=str(record["language"]), relation="definition",
            symbol=anchor.symbol, score=0.92,
            metadata={"conversation_anchor": True, "resolution": "anchor"},
        )
        return item if item.validate(self.root) else None

    @staticmethod
    def _dedupe(items: Iterable[Evidence]) -> List[Evidence]:
        resolution_priority = {
            "exact": 4,
            "enclosing": 4,
            "ambiguous_candidate": 3,
            "anchor": 2,
        }
        result: List[Evidence] = []
        positions: Dict[Tuple[str, int, int, str], int] = {}
        for item in items:
            key = (item.path, item.start_line, item.end_line, item.relation)
            previous = positions.get(key)
            if previous is None:
                positions[key] = len(result)
                result.append(item)
            else:
                existing = result[previous]
                incoming_priority = resolution_priority.get(
                    str(item.metadata.get("resolution") or ""), 0)
                existing_priority = resolution_priority.get(
                    str(existing.metadata.get("resolution") or ""), 0)
                if (incoming_priority > existing_priority
                        or (incoming_priority == existing_priority
                            and item.score > existing.score)):
                    # Resolution confidence dominates retrieval score. In
                    # particular, a fresh exact definition must replace a
                    # same-span conversation anchor after a source update.
                    result[previous] = item
        return result

    @staticmethod
    def _split_long(item: Evidence, focus_line: Optional[int] = None) -> List[Evidence]:
        line_count = item.end_line - item.start_line + 1
        if line_count <= 200:
            return [item]
        source_lines = item.content.splitlines()
        windows: List[Tuple[int, int]] = [(0, min(79, line_count - 1))]
        if focus_line is not None and item.start_line <= focus_line <= item.end_line:
            center = focus_line - item.start_line
            windows.append((max(0, center - 45), min(line_count - 1, center + 45)))
        windows.append((max(0, line_count - 35), line_count - 1))
        merged: List[Tuple[int, int]] = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        output: List[Evidence] = []
        for index, (start, end) in enumerate(merged):
            metadata = dict(item.metadata)
            metadata.update({
                "continuation": True,
                "continuation_index": index,
                "parent_span": [item.start_line, item.end_line],
                "truncated": True,
            })
            output.append(replace(
                item,
                start_line=item.start_line + start,
                end_line=item.start_line + end,
                content="\n".join(source_lines[start:end + 1]),
                metadata=metadata,
            ))
        return output

    def collect(
        self,
        question: str,
        current_file: str,
        selection: Optional[Tuple[int, int]] = None,
        anchors: Sequence[EvidenceAnchor] = (),
    ) -> ContextResult:
        # index_project is incremental: unchanged files are a cheap mtime/size pass.
        status = self.index.index_project(self.root)
        warnings = [f"部分 Python 文件解析失败：{path}: {message}"
                    for path, message in status.parse_errors.items()]
        evidence: List[Evidence] = []
        focus_line: Optional[int] = None
        if selection is not None:
            start, end = selection
            focus_line = start
            selected = self._selection(current_file, start, end)
            if selected is not None:
                evidence.append(selected)
            enclosing = self.index.resolve_symbol_rows(self.root, current_file, start, "", 1)
            if enclosing:
                item = self._from_symbol(enclosing[0], score=1.09, resolution="enclosing")
                if item is not None:
                    evidence.append(item)
        else:
            record = self.index.file_record(self.root, current_file)
            if record is not None and int(record.get("line_count") or 0) > 0:
                end = min(80, int(record["line_count"]))
                content = self._span(current_file, 1, end)
                if content is not None:
                    evidence.append(Evidence(
                        path=current_file, start_line=1, end_line=end, content=content,
                        source_hash=str(record["source_hash"]),
                        language=str(record["language"]), relation="text",
                        symbol=None, score=0.86,
                        metadata={"current_file_preview": True},
                    ))
        stale: List[EvidenceAnchor] = []
        for anchor in anchors[-16:]:
            item = self._anchor(anchor)
            if item is None:
                stale.append(anchor)
            else:
                evidence.append(item)
        if stale:
            self.index.refresh_paths(self.root, [anchor.path for anchor in stale])
            unresolved = 0
            for anchor in stale:
                item = self._anchor(anchor)
                if item is None:
                    unresolved += 1
                else:
                    evidence.append(item)
            if unresolved:
                warnings.append(f"已拒绝 {unresolved} 条源码已变化的会话 Evidence，并重新检索")

        identifiers = self._identifiers(question)
        exact_ids: List[int] = []
        resolved_paths = {current_file}
        ambiguous_identifiers: List[str] = []
        for identifier in identifiers:
            rows = self.index.resolve_symbol_rows(
                self.root, path=current_file, expression=identifier, limit=8
            )
            if len(rows) == 1:
                item = self._from_symbol(rows[0], score=1.08, resolution="exact")
                if item is not None:
                    evidence.append(item)
                    exact_ids.append(int(rows[0]["id"]))
                    resolved_paths.add(item.path)
                    evidence.extend(self.retriever._graph(identifier, "caller", 6))
                    evidence.extend(self.retriever._graph(identifier, "callee", 6))
            elif len(rows) > 1:
                ambiguous_identifiers.append(identifier)
                for row in rows[:4]:
                    item = self._from_symbol(
                        row, score=0.82, resolution="ambiguous_candidate"
                    )
                    if item is not None:
                        evidence.append(item)
                        resolved_paths.add(item.path)

        retrieved = self.retriever.retrieve(question, current_file=current_file, limit=18)
        if len(resolved_paths) > 1:
            # Once imports/scope identify candidate files, do not reintroduce every
            # global same-name definition or broad OR/substring hit as source context.
            # Graph evidence may legitimately introduce a caller/callee path.
            retrieved = [
                item for item in retrieved
                if item.path in resolved_paths
                or item.relation in {"caller", "callee"}
                or (
                    item.relation == "reference"
                    and item.metadata.get("resolution_status") == "resolved"
                )
            ]
        evidence.extend(retrieved)
        validated = [item for item in self._dedupe(evidence) if item.validate(self.root)]
        compacted: List[Evidence] = []
        for item in validated:
            compacted.extend(self._split_long(item, focus_line))

        needs_implementation = bool(_IMPLEMENTATION_TERMS.search(question))
        needs_relations = bool(_RELATION_TERMS.search(question))
        needs_multi_hop = bool(_MULTI_HOP_TERMS.search(question))
        caller_focus = bool(_CALLER_FOCUS_TERMS.search(question))
        exact_definition = any(
            item.relation == "definition" and item.metadata.get("resolution") in {"exact", "enclosing"}
            for item in compacted
        )
        has_relation = any(item.relation in {"reference", "caller", "callee"} for item in compacted)
        ambiguous = bool(ambiguous_identifiers)
        if ambiguous:
            sufficient, reason = False, "目标符号存在多个同名候选，必须继续消歧"
        elif needs_multi_hop:
            sufficient, reason = False, "问题要求多跳关系，确定性预取仅覆盖一跳，必须继续追踪"
        elif needs_implementation and not exact_definition:
            sufficient, reason = False, "实现类问题缺少精确定义"
        elif needs_relations and not has_relation:
            sufficient, reason = False, "调用或影响问题缺少引用/调用边"
        elif not compacted:
            sufficient, reason = False, "尚未找到可验证的源码证据"
        else:
            sufficient, reason = True, "确定性预取已覆盖问题所需证据"
        return ContextResult(
            evidence=compacted,
            repository_map=self.index.repository_map(self.root, current_file, self.repo_map_tokens),
            sufficient=sufficient,
            reason=reason,
            target_symbol_ids=list(dict.fromkeys(exact_ids)),
            warnings=warnings,
            index_revision=self.index.project_revision(self.root),
            direct_target=bool(
                exact_ids and not caller_focus and not needs_multi_hop and not ambiguous
            ),
            multi_hop=needs_multi_hop,
        )


__all__ = ["ContextBroker", "ContextResult"]
