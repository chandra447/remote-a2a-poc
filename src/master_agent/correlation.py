from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorrelationEntry:
    thread_id: str
    context_id: str | None = None


class CorrelationStore:
    """In-memory task_id → thread_id mapping.

    Sufficient for single-instance POC. Replace with DynamoDB for
    multi-instance ECS (see docs/a2a-async-orchestration.md §6.2).
    """

    def __init__(self) -> None:
        self._store: dict[str, CorrelationEntry] = {}

    def put(self, task_id: str, thread_id: str, context_id: str | None = None) -> None:
        self._store[task_id] = CorrelationEntry(thread_id=thread_id, context_id=context_id)

    def get(self, task_id: str) -> CorrelationEntry | None:
        return self._store.get(task_id)

    def delete(self, task_id: str) -> None:
        self._store.pop(task_id, None)


# Module-level singleton shared between tools and the webhook handler.
store = CorrelationStore()
