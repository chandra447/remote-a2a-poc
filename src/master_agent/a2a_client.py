from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import httpx

from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    MessageSendConfiguration,
    Part,
    PushNotificationConfig,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskPushNotificationConfig,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)


@dataclass(frozen=True)
class RemoteAgentResponse:
    text: str
    task_id: str | None
    context_id: str | None


class RemoteA2AAgent:
    """Thin wrapper around an a2a-sdk `Client` for a single remote agent.

    Lifecycle is two-phase so this object can be constructed on one event loop
    (e.g. at module import, for AgentCard discovery) and then *used* on a
    different loop (e.g. the langgraph-api server loop) without dragging stale
    loop references through the httpx connection pool.

    - `resolve_card()` uses a throwaway `httpx.AsyncClient` to fetch the card
      and closes that client before returning.
    - The real long-lived `httpx.AsyncClient` + A2A `Client` are built lazily
      on the *first* call to `ask()`, so they bind to whatever loop is then
      running.
    """

    def __init__(
        self,
        base_url: str,
        *,
        request_timeout_s: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout_s
        self._card: AgentCard | None = None
        self._http: httpx.AsyncClient | None = None
        self._client: Client | None = None
        self._init_lock = asyncio.Lock()

    @property
    def card(self) -> AgentCard | None:
        return self._card

    async def resolve_card(self) -> AgentCard:
        """Fetch and cache the `AgentCard`. Safe to call on any event loop."""
        if self._card is not None:
            return self._card
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resolver = A2ACardResolver(httpx_client=http, base_url=self._base_url)
            self._card = await resolver.get_agent_card()
        return self._card

    async def _ensure_client(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            if self._card is None:
                await self.resolve_card()
            assert self._card is not None
            self._http = httpx.AsyncClient(timeout=self._timeout)
            config = ClientConfig(
                httpx_client=self._http,
                streaming=False,
                accepted_output_modes=["text/plain"],
            )
            factory = ClientFactory(config)
            self._client = factory.create(self._card)
            return self._client

    async def send_nonblocking(
        self,
        prompt: str,
        *,
        webhook_url: str,
        context_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Fire-and-forget send — returns (task_id, context_id) immediately.

        Two-phase:
          1. message/send with blocking=False  →  server returns task immediately
             with state=working; background processing starts on the server.
          2. tasks/pushNotificationConfig/set  →  registers the webhook URL so
             the server POSTs the completed Task when background work is done.
        """
        client = await self._ensure_client()

        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=prompt))],
            context_id=context_id,
        )
        send_cfg = MessageSendConfiguration(
            blocking=False,
            accepted_output_modes=["text/plain"],
        )

        # Phase 1 — consume only the first event to capture task_id
        task_id: str | None = None
        task_context_id: str | None = None
        async for event in client.send_message(message, configuration=send_cfg):
            if isinstance(event, tuple):
                task, _ = event
                task_id = task.id
                task_context_id = task.context_id
                break  # don't wait for completion

        if task_id is None:
            raise RuntimeError("A2A specialist returned no initial task event")

        # Phase 2 — register webhook so specialist POSTs back when done
        await client.set_task_callback(
            TaskPushNotificationConfig(
                task_id=task_id,
                push_notification_config=PushNotificationConfig(url=webhook_url),
            )
        )

        return task_id, task_context_id

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            self._client = None

    async def ask(
        self,
        prompt: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
    ) -> RemoteAgentResponse:
        client = await self._ensure_client()

        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.user,
            parts=[Part(root=TextPart(text=prompt))],
            context_id=context_id,
            task_id=task_id,
        )

        final_text: list[str] = []
        final_task_id: str | None = None
        final_context_id: str | None = None

        async for event in client.send_message(message):
            text, tid, cid = _extract_event(event)
            if text:
                final_text.append(text)
            final_task_id = tid or final_task_id
            final_context_id = cid or final_context_id

        return RemoteAgentResponse(
            text="\n".join(t for t in final_text if t).strip(),
            task_id=final_task_id,
            context_id=final_context_id,
        )


def _extract_event(
    event: Message | tuple[Task, TaskStatusUpdateEvent | TaskArtifactUpdateEvent | None],
) -> tuple[str, str | None, str | None]:
    if isinstance(event, Message):
        return _join_parts(event.parts), event.task_id, event.context_id

    task, update = event
    text_chunks: list[str] = []

    if isinstance(update, TaskArtifactUpdateEvent):
        text_chunks.append(_join_parts(update.artifact.parts))

    if task.status.state == TaskState.completed:
        if task.artifacts:
            for artifact in task.artifacts:
                text_chunks.append(_join_parts(artifact.parts))
        if task.status.message is not None:
            text_chunks.append(_join_parts(task.status.message.parts))

    return "\n".join(c for c in text_chunks if c), task.id, task.context_id


def _join_parts(parts: list[Part]) -> str:
    out: list[str] = []
    for part in parts:
        root = part.root
        if isinstance(root, TextPart):
            out.append(root.text)
    return "\n".join(out)
