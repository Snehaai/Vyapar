"""
In-memory state stores for contexts and conversations.
Thread-safe enough for single-process FastAPI (asyncio).
"""
from typing import Any, Optional


class ContextStore:
    """Stores category / merchant / customer / trigger contexts by (scope, context_id)."""

    def __init__(self):
        # (scope, context_id) -> {version: int, payload: dict}
        self._data: dict[tuple[str, str], dict] = {}

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        entry = self._data.get((scope, context_id))
        return entry["version"] if entry else None

    def set(self, scope: str, context_id: str, version: int, payload: dict):
        self._data[(scope, context_id)] = {"version": version, "payload": payload}

    def get(self, scope: str, context_id: str) -> Optional[dict]:
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def all_of_scope(self, scope: str) -> dict[str, dict]:
        """Return {context_id: payload} for a given scope."""
        return {
            cid: entry["payload"]
            for (sc, cid), entry in self._data.items()
            if sc == scope
        }

    def counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _) in self._data:
            if scope in counts:
                counts[scope] += 1
        return counts

    def clear(self):
        self._data.clear()


class ConversationStore:
    """Stores conversation turn history by conversation_id."""

    def __init__(self):
        self._turns: dict[str, list[dict]] = {}

    def add_turn(self, conv_id: str, role: str, body: str):
        if conv_id not in self._turns:
            self._turns[conv_id] = []
        self._turns[conv_id].append({"role": role, "body": body})

    def get_turns(self, conv_id: str) -> list[dict]:
        return list(self._turns.get(conv_id, []))

    def clear(self):
        self._turns.clear()
