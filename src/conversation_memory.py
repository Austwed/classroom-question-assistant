"""In-memory conversation shared by voice and manual questions for one app run."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class MemoryTurn:
    record_id: int
    statement: str
    parent_id: int | None = None
    answer: str | None = None


class ConversationMemory:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[int, MemoryTurn] = {}

    def record_question(self, record_id: int, statement: str, parent_id: int | None = None) -> None:
        with self._lock:
            turn = self._turns.get(record_id)
            if turn is None:
                self._turns[record_id] = MemoryTurn(record_id, statement, parent_id)
            else:
                turn.statement = statement
                turn.parent_id = parent_id

    def record_answer(self, record_id: int, answer: str) -> None:
        with self._lock:
            if record_id in self._turns:
                self._turns[record_id].answer = answer

    def history_before(self, record_id: int) -> tuple[tuple[int, str, str | None], ...]:
        """Return the full active history, with corrections at their original position."""
        with self._lock:
            def root_of(turn_id: int) -> int:
                seen = set()
                while turn_id in self._turns and turn_id not in seen:
                    seen.add(turn_id)
                    parent_id = self._turns[turn_id].parent_id
                    if parent_id is None:
                        break
                    turn_id = parent_id
                return turn_id

            current_root = root_of(record_id)
            active: dict[int, MemoryTurn] = {}
            for turn_id in sorted(self._turns):
                if turn_id >= record_id:
                    break
                turn = self._turns[turn_id]
                if not turn.statement:
                    continue
                root_id = root_of(turn_id)
                if root_id != current_root:
                    active[root_id] = turn
            return tuple(
                (turn.record_id, turn.statement, turn.answer)
                for _, turn in sorted(active.items())
            )
