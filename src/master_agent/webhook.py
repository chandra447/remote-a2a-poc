from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from langgraph.types import Command

from master_agent.correlation import store as correlation_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Injected at startup by server.py once the graph is compiled.
_graph = None


def set_graph(graph) -> None:
    global _graph
    _graph = graph


@router.post("/webhook/a2a", status_code=202)
async def handle_a2a_callback(payload: dict) -> dict:
    """Receive a completed-task push notification from a specialist A2A server.

    The specialist POSTs the full Task JSON here when work finishes.
    We look up the suspended LangGraph thread and resume it.
    """
    task_id = payload.get("id")
    if not task_id:
        raise HTTPException(status_code=400, detail="payload missing 'id'")

    entry = correlation_store.get(task_id)
    if entry is None:
        # Could be a retried delivery for a task we already handled.
        logger.warning("webhook: unknown task_id=%s — ignoring", task_id)
        return {"status": "ignored", "reason": "unknown_task"}

    if _graph is None:
        raise HTTPException(status_code=503, detail="graph not initialised yet")

    text = _extract_text(payload)
    logger.info("webhook: task=%s resumed thread=%s", task_id, entry.thread_id)

    await _graph.ainvoke(
        Command(resume={"text": text, "task_id": task_id}),
        config={"configurable": {"thread_id": entry.thread_id}},
    )

    correlation_store.delete(task_id)
    return {"status": "resumed", "task_id": task_id}


def _extract_text(task: dict) -> str:
    """Pull text out of a Task dict returned by the A2A push notification."""
    chunks: list[str] = []

    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            text = _part_text(part)
            if text:
                chunks.append(text)

    if not chunks:
        status_msg = (task.get("status") or {}).get("message") or {}
        for part in status_msg.get("parts") or []:
            text = _part_text(part)
            if text:
                chunks.append(text)

    return "\n".join(chunks).strip()


def _part_text(part: dict) -> str:
    # A2A parts are either {"text": "..."} or {"root": {"text": "..."}}
    if isinstance(part.get("text"), str):
        return part["text"]
    root = part.get("root") or {}
    return root.get("text") or ""
