"""Process-local, project-scoped conversation memory without retained source text."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4


@dataclass(frozen=True)
class EvidenceAnchor:
    path: str
    start_line: int
    end_line: int
    source_hash: str
    symbol: Optional[str] = None

    @classmethod
    def from_value(cls, value: Any) -> Optional["EvidenceAnchor"]:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if not isinstance(value, Mapping):
            return None
        try:
            path = str(value.get("path") or "").replace("\\", "/")
            raw_start = value.get("start_line")
            raw_end = value.get("end_line")
            if raw_start is None or raw_end is None:
                return None
            start = int(raw_start)
            end = int(raw_end)
            source_hash = str(value.get("source_hash") or "")
        except (TypeError, ValueError):
            return None
        if not path or start < 1 or end < start:
            return None
        symbol = value.get("symbol")
        return cls(path, start, end, source_hash, str(symbol) if symbol else None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "source_hash": self.source_hash,
            "symbol": self.symbol,
        }


@dataclass(frozen=True)
class ConversationEvent:
    kind: str
    timestamp: float
    payload: Dict[str, Any]


@dataclass
class Conversation:
    conversation_id: str
    browser_session_id: str
    project_id: str
    active_path: str
    created_at: float
    updated_at: float
    events: List[ConversationEvent] = field(default_factory=list)
    anchors: List[EvidenceAnchor] = field(default_factory=list)
    checkpoint: str = ""

    def messages(self, complete_turns: int = 4) -> List[Dict[str, Any]]:
        values: List[Dict[str, Any]] = []
        for event in self.events:
            if event.kind not in {"user", "assistant"}:
                continue
            content = str(event.payload.get("content") or "")
            if content:
                message: Dict[str, Any] = {"role": event.kind, "content": content}
                anchors = event.payload.get("evidence")
                if isinstance(anchors, list) and anchors:
                    message["evidence"] = list(anchors)
                values.append(message)
        return values[-max(2, complete_turns * 2):]


class ConversationStore:
    """Thread-safe TTL/LRU memory bound to browser session and project."""

    def __init__(self, ttl_minutes: int = 120, max_sessions: int = 64) -> None:
        self.ttl_seconds = max(60, int(ttl_minutes) * 60)
        self.max_sessions = max(1, int(max_sessions))
        self._items: "OrderedDict[str, Conversation]" = OrderedDict()
        self._lock = threading.RLock()

    def reconfigure(self, ttl_minutes: int, max_sessions: int) -> None:
        with self._lock:
            self.ttl_seconds = max(60, int(ttl_minutes) * 60)
            self.max_sessions = max(1, int(max_sessions))
            self._prune(time.time())

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._items.items()
                   if now - value.updated_at > self.ttl_seconds]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self.max_sessions:
            self._items.popitem(last=False)

    @staticmethod
    def _client_messages(history: Sequence[Any]) -> Iterable[ConversationEvent]:
        for item in history[-8:]:
            value = item.model_dump() if hasattr(item, "model_dump") else item
            if not isinstance(value, Mapping):
                continue
            role = str(value.get("role") or "")
            content = str(value.get("content") or "")
            if role in {"user", "assistant"} and content:
                yield ConversationEvent(role, time.time(), {"content": content, "recovered": True})

    def get_or_create(
        self,
        conversation_id: Optional[str],
        browser_session_id: str,
        project_id: str,
        active_path: str,
        client_history: Sequence[Any] = (),
    ) -> Tuple[Conversation, bool]:
        now = time.time()
        with self._lock:
            self._prune(now)
            existing = self._items.get(str(conversation_id or ""))
            if (existing is not None
                    and existing.browser_session_id == browser_session_id
                    and existing.project_id == project_id):
                existing.active_path = active_path.replace("\\", "/")
                existing.updated_at = now
                self._items.move_to_end(existing.conversation_id)
                return existing, False
            created = Conversation(
                conversation_id=uuid4().hex,
                browser_session_id=browser_session_id,
                project_id=project_id,
                active_path=active_path.replace("\\", "/"),
                created_at=now,
                updated_at=now,
                events=list(self._client_messages(client_history)),
            )
            for message in client_history:
                value = message.model_dump() if hasattr(message, "model_dump") else message
                if isinstance(value, Mapping):
                    self._merge_anchors(created, value.get("evidence") or ())
            self._items[created.conversation_id] = created
            self._prune(now)
            return created, True

    @staticmethod
    def _merge_anchors(conversation: Conversation, values: Iterable[Any]) -> None:
        merged = list(conversation.anchors)
        for value in values:
            anchor = EvidenceAnchor.from_value(value)
            if anchor is None:
                continue
            key = (anchor.path, anchor.start_line, anchor.end_line, anchor.symbol)
            merged = [item for item in merged
                      if (item.path, item.start_line, item.end_line, item.symbol) != key]
            merged.append(anchor)
        conversation.anchors = merged[-16:]

    def append(self, conversation: Conversation, kind: str, payload: Mapping[str, Any]) -> None:
        if kind not in {"user", "tool_call", "tool_result", "assistant", "checkpoint"}:
            raise ValueError(f"unsupported conversation event: {kind}")
        now = time.time()
        with self._lock:
            if conversation.conversation_id not in self._items:
                return
            event_payload = dict(payload)
            if kind == "tool_result":
                # Store locators only. Source bodies remain in the active request.
                event_payload.pop("content", None)
                event_payload.pop("items", None)
            anchors = [
                anchor.to_dict() for anchor in (
                    EvidenceAnchor.from_value(value)
                    for value in event_payload.get("evidence") or ()
                ) if anchor is not None
            ]
            if "evidence" in event_payload:
                event_payload["evidence"] = anchors
            conversation.events.append(ConversationEvent(kind, now, event_payload))
            conversation.events = conversation.events[-64:]
            if kind == "checkpoint":
                conversation.checkpoint = str(event_payload.get("summary") or "")[:2048]
            self._merge_anchors(conversation, anchors)
            conversation.updated_at = now
            self._items.move_to_end(conversation.conversation_id)


__all__ = ["Conversation", "ConversationEvent", "ConversationStore", "EvidenceAnchor"]
