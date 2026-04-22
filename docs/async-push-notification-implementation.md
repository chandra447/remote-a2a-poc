# Async Push-Notification Pattern — Implementation Deep Dive

This document explains how the `feat/async-push-notification` branch upgrades the POC
from synchronous blocking calls to a fully async, webhook-driven flow using A2A push
notifications and LangGraph's `interrupt()` / `Command(resume=...)` primitives.

---

## Why This Exists

The original POC made synchronous A2A calls: the master agent held an HTTP connection
open until the specialist finished. This breaks for any specialist that does real work
(LLM calls, web search, database queries) because:

- The HTTP timeout fires before the specialist completes.
- One blocked connection per in-flight task limits throughput.
- In production, specialists live in different services (even different AWS landing
  zones), where holding a TCP connection across process boundaries is impractical.

The async pattern fixes this: the master **fires and forgets** the request, suspends
the LangGraph graph to disk, and resumes only when the specialist POSTs a callback.

---

## High-Level Flow

```
User
 │
 ▼
POST /chat  ──────────────────────────────────────────────────────────────────────┐
 │                                                                                │
 ▼                                                                                │
Master Agent (LangGraph)                                                          │
 │  LLM decides to delegate → calls peer tool                                    │
 │                                                                                │
 ▼                                                                                │
_make_async_tool._call()                                                          │
 │  1. send_nonblocking() → POST message/send (blocking=False)  ─────────────►  │
 │       Specialist returns task_id in < 1 s; background work starts             │
 │  2. set_task_callback() → POST tasks/pushNotificationConfig/set               │
 │       Registers webhook URL so specialist knows where to POST when done       │
 │  3. correlation_store.put(task_id, thread_id)                                 │
 │  4. interrupt({"task_id": ..., "status": "working"})                          │
 │       LangGraph checkpoints graph state to SQLite and suspends                │
 │                                                                                │
 ▼                                                                                │
POST /chat returns  {"status": "pending", "task_id": "...", "thread_id": "..."}  │
                                                                                  │
                    ┌─────────────────────────────────────────────────────────┐  │
                    │  Company Expert (specialist)                             │  │
                    │    • asyncio.sleep(ARTIFICIAL_DELAY_S)                  │  │
                    │    • LangChain agent runs Tavily web search             │  │
                    │    • Emits TaskArtifactUpdateEvent with result          │  │
                    │    • A2A SDK fires HTTP POST to webhook_url             │  │
                    └──────────────────┬──────────────────────────────────────┘  │
                                       │                                          │
                                       ▼                                          │
                    POST /webhook/a2a  (Task JSON payload)                        │
                     │                                                            │
                     ▼                                                            │
                    handle_a2a_callback()                                         │
                     │  Returns 202 immediately (avoids specialist timeout)       │
                     │  BackgroundTask: _resume_graph()                           │
                     │    • _extract_text(payload) → specialist result text      │
                     │    • correlation_store.get(task_id) → thread_id           │
                     │    • graph.ainvoke(Command(resume={"text": ...}), ...)    │
                     │       interrupt() unblocks; tool returns specialist text   │
                     │       LLM synthesizes final answer                         │
                     │    • correlation_store.delete(task_id)                    │
                                                                                  │
GET /chat/{thread_id}  ◄──────────────────────────────────────────────────────────┘
 │  snapshot.next == []  →  status="completed", reply="..."
```

---

## Component-by-Component Explanation

### 1. Company Expert — Push Notification Support (`main.py`)

Two changes were needed on the server side:

**`AgentCapabilities(push_notifications=True)`**

The A2A `JSONRPCHandler` checks `agent_card.capabilities.push_notifications` before
forwarding `tasks/pushNotificationConfig/set` to the handler. Without this flag, every
callback registration is rejected with *"Push notifications are not supported by the
agent"* — before even reaching `DefaultRequestHandler`.

**`InMemoryPushNotificationConfigStore` + `BasePushNotificationSender`**

`DefaultRequestHandler` guards its push-notification methods with
`if not self._push_config_store: raise UnsupportedOperationError()`. Both the config
store and sender must be wired in at construction:

```python
push_config_store = InMemoryPushNotificationConfigStore()
_http = httpx.AsyncClient()
push_sender = BasePushNotificationSender(_http, push_config_store)

handler = DefaultRequestHandler(
    agent_executor=CompanyExpertExecutor(),
    task_store=InMemoryTaskStore(),
    queue_manager=InMemoryQueueManager(),
    push_config_store=push_config_store,   # ← required
    push_sender=push_sender,               # ← required
)
```

The `httpx.AsyncClient` is closed in a FastAPI lifespan to avoid resource leaks.

---

### 2. Company Expert — Artificial Delay (`executor.py`)

The executor sleeps before running the LangChain agent to simulate a long-running
specialist. Controlled by the `ARTIFICIAL_DELAY_S` env var (default 300 s in prod,
10 s for local testing):

```python
await asyncio.sleep(_settings.artificial_delay_s)
result = await finance_agent.ainvoke(...)
```

**Result emission — `TaskArtifactUpdateEvent` not `Message`**

The A2A push notification payload is a serialised `Task` object. The SDK builds the
task's `artifacts` list from `TaskArtifactUpdateEvent`s, not from bare `Message`
events. Emitting a `Message` leaves `task.artifacts` empty and the webhook receives
no extractable text. The fix:

```python
await event_queue.enqueue_event(
    TaskArtifactUpdateEvent(
        task_id=context.task_id,
        context_id=context.context_id,
        artifact=Artifact(
            artifact_id=str(uuid.uuid4()),
            parts=[Part(root=TextPart(text=output))],
        ),
        append=False,
        last_chunk=True,
    )
)
```

---

### 3. Master Agent — Non-Blocking Send (`a2a_client.py`)

`send_nonblocking()` is a two-phase operation:

**Phase 1 — fire the request**

```python
send_cfg = MessageSendConfiguration(blocking=False, ...)
async for event in client.send_message(message, configuration=send_cfg):
    if isinstance(event, tuple):
        task, _ = event
        task_id = task.id   # server returns immediately with state=working
        break
```

`blocking=False` tells the A2A server to accept the task and return right away rather
than waiting for completion. The first (and only) event we care about is the initial
`Task` object, which gives us the `task_id`.

**Phase 2 — register the webhook**

```python
await client.set_task_callback(
    TaskPushNotificationConfig(
        task_id=task_id,
        push_notification_config=PushNotificationConfig(url=webhook_url),
    )
)
```

This calls `tasks/pushNotificationConfig/set` on the specialist. When the specialist
finishes, the A2A SDK (`BasePushNotificationSender`) reads the registered URL and
POSTs the completed `Task` JSON there.

> **Pitfall:** The field is `task_id=`, not `id=`. The SDK's Pydantic model uses
> `task_id` but some documentation shows `id`. Using the wrong name silently passes
> validation and registers nothing.

---

### 4. Master Agent — Async Tool with `interrupt()` (`tools.py`)

```python
@tool(tool_name, description=description)
async def _call(question: str, config: RunnableConfig) -> str:
    thread_id = (config.get("configurable") or {}).get("thread_id")

    task_id, context_id = await remote.send_nonblocking(
        question, webhook_url=webhook_url
    )

    # Register correlation BEFORE interrupt to avoid race where a fast
    # specialist posts the webhook before the store entry exists.
    correlation_store.put(task_id, thread_id, context_id)

    # Suspend the graph here. LangGraph writes state to the checkpointer.
    # The webhook handler will call Command(resume=...) to unblock this.
    resume_payload: dict = interrupt({"task_id": task_id, "status": "working"})

    return resume_payload.get("text", "(specialist returned no text)")
```

**Critical import placement**

`RunnableConfig` and `interrupt` must be imported at module level, not inside the
function. The `@tool` decorator calls `get_type_hints()` with the function's
`__globals__` to resolve type annotations. A local import is not in `__globals__`
and causes `NameError: name 'RunnableConfig' is not defined` at startup.

---

### 5. Master Agent — Correlation Store (`correlation.py`)

A simple in-memory mapping from `task_id` to `(thread_id, context_id)`:

```
correlation_store.put(task_id, thread_id, context_id)   # set before interrupt
correlation_store.get(task_id)  →  CorrelationEntry     # read in webhook handler
correlation_store.delete(task_id)                        # cleanup after resume
```

In a multi-instance production deployment this would be DynamoDB or Redis. For this
single-process POC an in-memory dict is sufficient.

---

### 6. Master Agent — Webhook Handler (`webhook.py`)

```python
@router.post("/webhook/a2a", status_code=202)
async def handle_a2a_callback(payload: dict, background_tasks: BackgroundTasks):
    task_id = payload.get("id")
    entry = correlation_store.get(task_id)
    text = _extract_text(payload)

    background_tasks.add_task(_resume_graph, task_id, entry.thread_id, text)
    return {"status": "accepted", "task_id": task_id}
```

**Why `BackgroundTasks`?**

The specialist's `httpx.AsyncClient` has a default timeout. If the webhook handler
runs `graph.ainvoke(...)` synchronously, it blocks while the master LLM generates
its synthesis — which can take 10–30 seconds. The specialist's HTTP client fires a
`ReadTimeout` and marks the push as failed. Returning 202 immediately and continuing
in a FastAPI `BackgroundTask` prevents this.

**Text extraction**

The push-notification payload is the A2A `Task` JSON. Text lives in
`task.artifacts[].parts[].text`:

```python
for artifact in task.get("artifacts") or []:
    for part in artifact.get("parts") or []:
        if isinstance(part.get("text"), str):
            chunks.append(part["text"])
        elif isinstance((part.get("root") or {}).get("text"), str):
            chunks.append(part["root"]["text"])
```

---

### 7. Master Agent — HTTP Server (`server.py`)

Three endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Start or continue a conversation. Returns `pending` immediately if the graph suspends waiting for a specialist. |
| `GET /chat/{thread_id}` | Poll for completion. Returns `completed` with `reply` when done, `pending` with `task_id` while waiting. |
| `POST /webhook/a2a` | Receives specialist push notifications. Resumes the suspended graph. |

**Extracting the interrupt value**

When the graph is suspended, `snapshot.next` is non-empty and `snapshot.tasks` holds
`Interrupt` objects. The `task_id` stored by the tool is recoverable:

```python
for pending_task in snapshot.tasks:
    for interrupt_obj in getattr(pending_task, "interrupts", []):
        val = getattr(interrupt_obj, "value", None) or {}
        if isinstance(val, dict) and val.get("task_id"):
            return val["task_id"], val.get("context_id")
```

**Choosing the reply**

After resume, the message list looks like:

```
[0] HumanMessage   — user's question
[1] AIMessage      — tool_calls=[...], content=""   (LLM decided to delegate)
[2] ToolMessage    — specialist result (3000+ chars)
[3] AIMessage      — LLM synthesis, content="..."   (may be terse)
```

`_last_text()` returns the final AIMessage when its content is substantial. If the
LLM only echoes a short meta-response (< half the ToolMessage length), the
ToolMessage content is returned directly so the user sees the actual specialist data.

---

## Key Bugs Fixed During Implementation

| Bug | Root Cause | Fix |
|---|---|---|
| *"Push notifications are not supported"* | `AgentCapabilities` missing `push_notifications=True` | Added flag to `AgentCard` in `main.py` |
| Same error after capability fix | `DefaultRequestHandler` constructed without `push_config_store` / `push_sender` | Wired both into `DefaultRequestHandler` |
| `NameError: 'RunnableConfig' is not defined` | Import inside `_make_async_tool` not in `__globals__` | Moved to module level in `tools.py` |
| `MasterSettings` validation error on startup | `load_dotenv()` in `__main__.py` ran after `__init__.py` eagerly imported `agent.py` → `MasterSettings()` | Cleared `__init__.py`; added `load_dotenv()` to `settings.py` |
| Specialist push timing out | Webhook handler blocked on `graph.ainvoke()` while LLM synthesised | Moved graph resume to FastAPI `BackgroundTask` |
| Empty ToolMessage content | Executor emitting `Message` event; push notification payload only captures `TaskArtifactUpdateEvent` | Switched executor to `TaskArtifactUpdateEvent` |
| Reply returned user's own question | `_last_text()` fell back to `HumanMessage` when all AIMessages were empty | Added `HumanMessage` skip + `tool_calls` skip |

---

## Running Locally

### Prerequisites

```
Python 3.13+   uv   OpenAI API key   Tavily API key
```

### `.env`

```env
MODEL_NAME=gpt-4o-mini
MODEL_PROVIDER=openai
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...

# Specialist server
AGENT_HOST=0.0.0.0
AGENT_PORT=8001
ARTIFICIAL_DELAY_S=10        # seconds to simulate long-running work

# Master agent
MASTER_HOST=0.0.0.0
MASTER_PORT=8000
A2A_PEER_URLS=http://localhost:8001
WEBHOOK_URL=http://localhost:8000/webhook/a2a
A2A_REQUEST_TIMEOUT_S=30
CHECKPOINT_DB_PATH=.data/master_agent_checkpoints.sqlite
```

### Start both servers

```bash
# Terminal 1 — specialist
uv run python main.py

# Terminal 2 — master
uv run python -m master_agent
```

### Send a request and poll

```bash
# 1. Fire the request — returns immediately with status=pending
curl -s -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "Compare Nvidia FY25 Q1 and Q2 earnings"}' | jq .

# {
#   "thread_id": "abc123...",
#   "status": "pending",
#   "task_id": "def456...",
#   "context_id": null,
#   "reply": null
# }

# 2. Poll until status=completed (ARTIFICIAL_DELAY_S + LLM latency)
curl -s http://localhost:8000/chat/<thread_id> | jq .

# {
#   "thread_id": "abc123...",
#   "status": "completed",
#   "task_id": null,
#   "context_id": null,
#   "reply": "..."
# }
```

---

## What This Does Not Cover (Production Gaps)

| Gap | Production Solution |
|---|---|
| In-memory correlation store | DynamoDB or Redis (survives restarts, works across instances) |
| SQLite checkpointer | `AsyncPostgresSaver` on RDS (multi-instance safe) |
| Single-process master | Multiple ECS tasks behind ALB; all share the Postgres checkpointer |
| No webhook auth | AWS API Gateway + SigV4 / shared HMAC secret |
| No retry on webhook delivery failure | SQS in front of webhook endpoint; A2A SDK retries with backoff |
| No task deadline / cancellation | `asyncio.wait_for` around specialist call + A2A `tasks/cancel` |

See [`docs/a2a-async-orchestration.md`](a2a-async-orchestration.md) for the full
production architecture design.
