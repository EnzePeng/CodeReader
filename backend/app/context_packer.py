"""Token-budget-aware packing of ranked source evidence."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Awaitable, Callable, List, Sequence

from .evidence import Evidence

_RELATION_PRIORITY = {
    "definition": 0,
    "reference": 1,
    "caller": 2,
    "callee": 3,
    "text": 4,
}


@dataclass(frozen=True)
class PackedContext:
    text: str
    evidence: List[Evidence]
    omitted: List[Evidence]
    used_tokens: int
    evidence_tokens: int
    reserved_tokens: int
    available_tokens: int
    warning: str = ""

    def to_dict(self):
        return {
            "text": self.text,
            "evidence": [item.to_dict() for item in self.evidence],
            "omitted": [item.to_dict() for item in self.omitted],
            "used_tokens": self.used_tokens,
            "evidence_tokens": self.evidence_tokens,
            "reserved_tokens": self.reserved_tokens,
            "available_tokens": self.available_tokens,
            "warning": self.warning,
        }


class ContextPacker:
    """Pack evidence after reserving model output, system and history tokens."""

    def __init__(
        self,
        token_counter: Callable[[str], int],
        context_window_tokens: int,
        output_reserve_tokens: int,
        system_reserve_tokens: int,
        history_reserve_tokens: int,
    ) -> None:
        self.token_counter = token_counter
        self.context_window_tokens = max(0, int(context_window_tokens))
        self.output_reserve_tokens = max(0, int(output_reserve_tokens))
        self.system_reserve_tokens = max(0, int(system_reserve_tokens))
        self.history_reserve_tokens = max(0, int(history_reserve_tokens))

    @staticmethod
    def _render(item: Evidence) -> str:
        symbol = f" · {item.symbol}" if item.symbol else ""
        return (
            f"[{item.relation}] {item.path}:{item.start_line}-{item.end_line}{symbol}\n"
            f"{item.content}"
        )

    @staticmethod
    def _fragments(evidence: Sequence[Evidence]) -> List[Evidence]:
        """Split oversized spans into independently packable, exact line ranges."""
        output: List[Evidence] = []
        for item in evidence:
            lines = item.content.splitlines()
            expected = item.end_line - item.start_line + 1
            if expected <= 120 or len(lines) != expected:
                output.append(item)
                continue
            for index, offset in enumerate(range(0, len(lines), 100)):
                fragment_lines = lines[offset:offset + 100]
                metadata = dict(item.metadata)
                metadata.update({
                    "continuation": True,
                    "continuation_index": index,
                    "parent_span": [item.start_line, item.end_line],
                })
                output.append(replace(
                    item,
                    start_line=item.start_line + offset,
                    end_line=item.start_line + offset + len(fragment_lines) - 1,
                    content="\n".join(fragment_lines),
                    score=max(0.0, item.score - index * 0.002),
                    metadata=metadata,
                ))
        return output

    def pack(self, evidence: Sequence[Evidence]) -> PackedContext:
        reserve_requested = (
            self.output_reserve_tokens
            + self.system_reserve_tokens
            + self.history_reserve_tokens
        )
        reserved = min(self.context_window_tokens, reserve_requested)
        available = max(0, self.context_window_tokens - reserve_requested)
        ordered = sorted(
            self._fragments(evidence),
            key=lambda item: (
                _RELATION_PRIORITY.get(item.relation, 99),
                -float(item.score),
                item.path.casefold(),
                item.start_line,
            ),
        )

        selected: List[Evidence] = []
        omitted: List[Evidence] = []
        blocks: List[str] = []
        for item in ordered:
            candidate_blocks = blocks + [self._render(item)]
            candidate = "\n\n".join(candidate_blocks)
            if self.token_counter(candidate) <= available:
                selected.append(item)
                blocks = candidate_blocks
            else:
                omitted.append(item)

        text = "\n\n".join(blocks)
        evidence_tokens = min(available, max(0, int(self.token_counter(text))))
        used = min(self.context_window_tokens, reserved + evidence_tokens)
        warnings: List[str] = []
        if reserve_requested >= self.context_window_tokens and evidence:
            warnings.append("系统、历史与输出预留已耗尽上下文预算")
        if omitted:
            warnings.append(f"受 token 预算限制，已省略 {len(omitted)} 条证据")
        return PackedContext(
            text=text,
            evidence=selected,
            omitted=omitted,
            used_tokens=used,
            evidence_tokens=evidence_tokens,
            reserved_tokens=reserved,
            available_tokens=available,
            warning="；".join(warnings),
        )

    async def pack_async(
        self,
        evidence: Sequence[Evidence],
        token_counter: Callable[[str], Awaitable[int]],
    ) -> PackedContext:
        """Pack with the active model tokenizer instead of a heuristic counter."""
        reserve_requested = (
            self.output_reserve_tokens
            + self.system_reserve_tokens
            + self.history_reserve_tokens
        )
        reserved = min(self.context_window_tokens, reserve_requested)
        available = max(0, self.context_window_tokens - reserve_requested)
        ordered = sorted(
            self._fragments(evidence),
            key=lambda item: (
                _RELATION_PRIORITY.get(item.relation, 99),
                -float(item.score),
                item.path.casefold(),
                item.start_line,
            ),
        )
        selected: List[Evidence] = []
        omitted: List[Evidence] = []
        blocks: List[str] = []
        for item in ordered:
            candidate_blocks = blocks + [self._render(item)]
            candidate = "\n\n".join(candidate_blocks)
            if await token_counter(candidate) <= available:
                selected.append(item)
                blocks = candidate_blocks
            else:
                omitted.append(item)
        text = "\n\n".join(blocks)
        evidence_tokens = min(available, max(0, int(await token_counter(text)))) if text else 0
        warnings: List[str] = []
        if reserve_requested >= self.context_window_tokens and evidence:
            warnings.append("系统、历史与输出预留已耗尽上下文预算")
        if omitted:
            warnings.append(f"受 token 预算限制，已省略 {len(omitted)} 条证据")
        return PackedContext(
            text=text,
            evidence=selected,
            omitted=omitted,
            used_tokens=min(self.context_window_tokens, reserved + evidence_tokens),
            evidence_tokens=evidence_tokens,
            reserved_tokens=reserved,
            available_tokens=available,
            warning="；".join(warnings),
        )


__all__ = ["ContextPacker", "PackedContext"]
