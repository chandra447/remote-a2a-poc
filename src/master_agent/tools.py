from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt

from master_agent.a2a_client import RemoteA2AAgent
from master_agent.correlation import store as correlation_store

logger = logging.getLogger(__name__)

_TOOL_NAME_SLUG = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass(frozen=True)
class PeerTool:
    """A LangChain tool bound to a discovered A2A peer."""

    remote: RemoteA2AAgent
    tool: BaseTool


def _slug(name: str) -> str:
    slug = _TOOL_NAME_SLUG.sub("_", name.strip().lower()).strip("_")
    return slug or "a2a_peer"


def _describe_card(remote: RemoteA2AAgent) -> str:
    card = remote.card
    assert card is not None, "card must be resolved before describing"

    skills = "\n".join(
        f"  - {s.name} ({s.id}): {s.description}" for s in (card.skills or [])
    ) or "  (no declared skills)"

    examples: list[str] = []
    for skill in card.skills or []:
        for example in (skill.examples or [])[:2]:
            examples.append(f"  - {example}")
    example_block = (
        "\nExample questions this agent handles well:\n" + "\n".join(examples)
        if examples
        else ""
    )

    return (
        f"Delegate a self-contained question to the remote A2A agent "
        f'"{card.name}" (v{card.version}).\n\n'
        f"Agent description: {card.description}\n\n"
        f"Skills:\n{skills}"
        f"{example_block}\n\n"
        "The remote agent has its own tools and knowledge; it does NOT share "
        "this conversation's memory. Pass a standalone prompt with all needed context."
    )


async def build_async_peer_tools(
    urls: list[str],
    *,
    request_timeout_s: float,
    webhook_url: str,
) -> list[PeerTool]:
    """Build interrupt-based async tools backed by A2A push-notification webhooks.

    Each tool sends a non-blocking message/send, suspends the LangGraph graph
    via interrupt(), and resumes when the webhook fires.
    """
    peers: list[PeerTool] = []
    used_names: set[str] = set()

    for url in urls:
        remote = RemoteA2AAgent(url, request_timeout_s=request_timeout_s)
        try:
            await remote.resolve_card()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping A2A peer %s: %s", url, exc)
            await remote.aclose()
            continue

        card = remote.card
        assert card is not None
        base = _slug(card.name)
        name = base
        i = 2
        while name in used_names:
            name = f"{base}_{i}"
            i += 1
        used_names.add(name)

        description = _describe_card(remote)
        peers.append(
            PeerTool(remote=remote, tool=_make_async_tool(remote, name, description, webhook_url))
        )

    return peers


def _make_async_tool(
    remote: RemoteA2AAgent,
    tool_name: str,
    description: str,
    webhook_url: str,
) -> BaseTool:
    @tool(tool_name, description=description)
    async def _call(question: str, config: RunnableConfig) -> str:
        thread_id = (config.get("configurable") or {}).get("thread_id")
        if not thread_id:
            raise ValueError("thread_id missing from LangGraph config")

        peer_name = remote.card.name if remote.card else "unknown"

        # 1. Non-blocking send — returns in <1s with task_id
        try:
            task_id, context_id = await remote.send_nonblocking(
                question, webhook_url=webhook_url
            )
        except Exception as exc:
            logger.error("send_nonblocking to %s failed: %s", peer_name, exc, exc_info=True)
            raise

        logger.info(
            "task_id=%s context_id=%s submitted to %s — suspending graph, "
            "waiting for webhook at %s",
            task_id, context_id, peer_name, webhook_url,
        )

        # 2. Register correlation BEFORE interrupt to avoid a race where a fast
        #    specialist posts the webhook before the correlation is stored.
        correlation_store.put(task_id, thread_id, context_id)

        # 3. Suspend the LangGraph graph here.  LangGraph checkpoints the state.
        #    The webhook handler will call Command(resume=...) to continue.
        resume_payload: dict = interrupt({"task_id": task_id, "status": "working"})

        logger.info("graph resumed for task_id=%s thread_id=%s", task_id, thread_id)
        return resume_payload.get("text", "(specialist returned no text)")

    return _call


async def build_peer_tools(
    urls: list[str],
    *,
    request_timeout_s: float,
) -> list[PeerTool]:
    """Resolve each URL's AgentCard and produce one LangChain tool per peer.

    Peers whose cards cannot be resolved are skipped with a warning so a dead
    peer doesn't take the whole master agent down.
    """
    peers: list[PeerTool] = []
    used_names: set[str] = set()

    for url in urls:
        remote = RemoteA2AAgent(url, request_timeout_s=request_timeout_s)
        try:
            await remote.resolve_card()
        except Exception as exc:  # noqa: BLE001 — surface any card-resolution failure
            logger.warning("skipping A2A peer %s: %s", url, exc)
            await remote.aclose()
            continue

        card = remote.card
        assert card is not None
        base = _slug(card.name)
        name = base
        i = 2
        while name in used_names:
            name = f"{base}_{i}"
            i += 1
        used_names.add(name)

        description = _describe_card(remote)
        peers.append(PeerTool(remote=remote, tool=_make_tool(remote, name, description)))

    return peers


def _make_tool(remote: RemoteA2AAgent, tool_name: str, description: str) -> BaseTool:
    @tool(tool_name, description=description)
    async def _call(question: str) -> str:
        response = await remote.ask(question)
        return response.text or "(the remote agent returned no text)"

    return _call
