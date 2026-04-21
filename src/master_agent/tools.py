from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from langchain_core.tools import BaseTool, tool

from master_agent.a2a_client import RemoteA2AAgent

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
