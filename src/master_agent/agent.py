from __future__ import annotations

import asyncio
import logging

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.base import BaseCheckpointSaver

from master_agent.settings import MasterSettings
from master_agent.tools import PeerTool, build_peer_tools

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Master Agent — an orchestrator that collaborates \
with specialist agents over the A2A (Agent-to-Agent) protocol.

You have been connected to a set of remote A2A agents at runtime. Each agent is \
exposed to you as a tool; the tool's name, description, declared skills, and \
example questions describe what that agent is good at. Read the tool schemas \
to decide which specialist (if any) is the right fit for a given user request.

**How to work:**
- When the user's question matches a remote specialist's declared skills, \
delegate to that specialist. Pass a self-contained prompt that includes every \
detail the specialist needs — remote agents do NOT share this conversation's \
memory.
- When no remote specialist is a good match, answer directly from your own \
knowledge and reasoning.
- You may chain calls across multiple specialists if a question has several \
sub-parts.
- When summarizing a specialist's response for the user, preserve concrete \
facts, numbers, and citations that the specialist returned. Do not invent \
data the specialist did not provide.
- If a tool call fails or returns an empty result, tell the user what \
happened rather than fabricating a plausible answer.

**Constraints:**
- Never invent financial figures, earnings, or sources.
- Do not provide personalized investment advice."""


def _build_chat_model(settings: MasterSettings):
    model_kwargs: dict[str, str] = {}
    if settings.llm_provider == "ollama":
        if settings.ollama_host:
            model_kwargs["base_url"] = settings.ollama_host
    elif settings.openai_base_url:
        model_kwargs["base_url"] = settings.openai_base_url

    return init_chat_model(
        model=settings.llm_model,
        model_provider=settings.llm_provider,
        **model_kwargs,
    )


async def build_master_agent_async(
    settings: MasterSettings | None = None,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    peer_tools: list[PeerTool] | None = None,
):
    """Async builder — resolves A2A peer cards unless `peer_tools` is provided.

    Returns `(agent, peer_tools)` so the caller can reuse the peer connections
    across invocations and close them at shutdown.
    """
    settings = settings or MasterSettings()
    if peer_tools is None:
        peer_tools = await build_peer_tools(
            settings.peer_urls, request_timeout_s=settings.a2a_request_timeout_s
        )

    if not peer_tools:
        logger.warning(
            "master agent starting with zero remote peers — check A2A_PEER_URLS"
        )

    kwargs: dict = {
        "model": _build_chat_model(settings),
        "tools": [pt.tool for pt in peer_tools],
        "system_prompt": SYSTEM_PROMPT,
        "name": "master_agent",
    }
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer

    return create_agent(**kwargs), peer_tools


def _build_graph_for_cli():
    """Blocking graph factory for `langgraph dev` / `langgraph.json`.

    The CLI imports this module once at startup. We resolve peer AgentCards
    synchronously here so the LLM sees tool schemas sourced from live A2A
    discovery rather than a hardcoded list.
    """
    agent, _peers = asyncio.run(build_master_agent_async())
    return agent


graph = _build_graph_for_cli()
