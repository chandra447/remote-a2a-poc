"""Master Agent HTTP server.

Exposes three endpoints:
  POST /chat              — start or continue a conversation
  GET  /chat/{thread_id}  — poll for result (returns pending until webhook fires)
  POST /webhook/a2a       — receives push-notification callbacks from specialists
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from master_agent import webhook
from master_agent.agent import build_master_agent_async
from master_agent.settings import MasterSettings
from master_agent.tools import build_async_peer_tools

load_dotenv()
logger = logging.getLogger(__name__)

_settings = MasterSettings()
_graph = None
_checkpointer = None
_peer_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph, _checkpointer, _peer_tools

    db_path = Path(_settings.checkpoint_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as cp:
        _checkpointer = cp

        _peer_tools = await build_async_peer_tools(
            _settings.peer_urls,
            request_timeout_s=_settings.a2a_request_timeout_s,
            webhook_url=_settings.webhook_url,
        )

        if not _peer_tools:
            logger.warning("no reachable A2A peers — master will answer directly")
        else:
            for pt in _peer_tools:
                card = pt.remote.card
                logger.info("discovered peer: %s [tool: %s]", card.name if card else "?", pt.tool.name)

        _graph, _ = await build_master_agent_async(
            _settings, checkpointer=cp, peer_tools=_peer_tools
        )
        webhook.set_graph(_graph)

        yield

    for pt in _peer_tools:
        await pt.remote.aclose()


app = FastAPI(title="Master Agent", lifespan=lifespan)
app.include_router(webhook.router)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    status: str   # "completed" | "pending"
    reply: str | None = None


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Send a message. Returns immediately — status='pending' if the graph is
    waiting for a specialist callback, 'completed' if it answered directly."""
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    await _graph.ainvoke(
        {"messages": [HumanMessage(content=req.message)]},
        config=config,
    )

    snapshot = await _graph.aget_state(config)

    if snapshot.next:
        logger.info("thread=%s suspended — waiting for specialist webhook", thread_id)
        return ChatResponse(thread_id=thread_id, status="pending")

    messages = snapshot.values.get("messages", [])
    reply = messages[-1].content if messages else None
    return ChatResponse(thread_id=thread_id, status="completed", reply=reply)


@app.get("/chat/{thread_id}", response_model=ChatResponse)
async def poll_chat(thread_id: str) -> ChatResponse:
    """Poll for a conversation result. Keep calling until status='completed'."""
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await _graph.aget_state(config)

    if not snapshot.values:
        return ChatResponse(thread_id=thread_id, status="not_found")

    if snapshot.next:
        return ChatResponse(thread_id=thread_id, status="pending")

    messages = snapshot.values.get("messages", [])
    reply = messages[-1].content if messages else None
    return ChatResponse(thread_id=thread_id, status="completed", reply=reply)
