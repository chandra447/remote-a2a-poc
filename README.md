# A2A Protocol POC — Master Agent + Specialist Agent Orchestration

A proof-of-concept exploring **Google's Agent-to-Agent (A2A) Protocol** for
connecting a LangGraph master/orchestrator agent to remote specialist agents
across independent services.

---

## What This Explores

| Question | Answer from this POC |
|---|---|
| Can a LangGraph agent discover and call remote agents at runtime? | Yes — via A2A AgentCard discovery |
| Does A2A give the master agent enough context to route intelligently? | Yes — skills, descriptions, and examples from the card go directly into the LLM's tool schema |
| Can specialist agents be deployed independently with no master-side code change? | Yes — master reads the card from `/.well-known/agent-card.json` and auto-creates a LangChain tool |
| Does OpenTelemetry tracing span across both agents? | Partial — traces propagate via `traceparent` header; tool-call spans inside the specialist are in a child span |
| Can this scale to async, long-running specialists in different AWS landing zones? | Designed but not implemented here — see [`docs/a2a-async-orchestration.md`](docs/a2a-async-orchestration.md) |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Master Agent  (LangGraph · LangChain · langgraph dev)   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  build_peer_tools()                              │   │
│  │  • fetches /.well-known/agent-card.json          │   │
│  │  • creates one LangChain tool per remote peer    │   │
│  │  • tool description = card skills + examples     │   │
│  └──────────────────────────────────────────────────┘   │
│                         │                                │
│             LLM decides which tool to call               │
│                         │                                │
│  ┌──────────────────────▼───────────────────────────┐   │
│  │  RemoteA2AAgent.ask()                            │   │
│  │  • POST /  (A2A JSON-RPC message/send)           │   │
│  │  • streams or polls until task completes         │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
                          │  HTTPS  (A2A JSON-RPC)
┌──────────────────────────────────────────────────────────┐
│  Company Expert Agent  (FastAPI · A2A SDK · Tavily)      │
│                                                          │
│  GET  /.well-known/agent-card.json   (skill discovery)   │
│  POST /   (A2A JSON-RPC handler)                         │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  LangChain agent with Tavily web search          │   │
│  │  • company financials & research                 │   │
│  │  • competitive analysis                          │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### Components

| Component | Location | Role |
|---|---|---|
| `company_expert` | `src/company_expert/` | Specialist A2A server — company financial research via Tavily |
| `master_agent` | `src/master_agent/` | Orchestrator — discovers peers, routes user questions |
| `main.py` | root | Entry point for `company_expert` A2A server |
| `langgraph.json` | root | Config for `langgraph dev` (runs master agent studio) |
| `docs/a2a-async-orchestration.md` | `docs/` | Production async design for long-running cross-LZ specialists |

---

## Key Findings

### What worked well

1. **Zero-coupling onboarding** — adding a new specialist requires only a URL. The
   master fetches the AgentCard at startup and auto-generates a typed LangChain
   tool. No master code changes needed.

2. **Skill-based routing** — the LLM reads the card's `skills[].description` and
   `examples` fields to decide which specialist to call. Routing was accurate
   for clearly-scoped specialist domains.

3. **A2A protocol abstraction** — the SDK handles JSON-RPC, task lifecycle
   (`submitted → working → completed`), and streaming. The application code
   only deals with `question → answer`.

4. **OpenTelemetry propagation** — `traceparent` flows from master → specialist
   via HTTP headers. Both agents appear in the same Motel/OTLP trace.

### Limitations found (and how to solve them)

| Limitation | Root Cause | Solution (see async doc) |
|---|---|---|
| Synchronous blocking on long tasks | Master holds HTTP connection while specialist works | A2A `blocking=false` + push notification webhook |
| Single-instance state (SQLite checkpointer) | LangGraph SQLite is local-only | Switch to `AsyncPostgresSaver` on RDS |
| No cross-LZ auth on webhook callbacks | Not needed for local POC | AWS SigV4 + IAM on API Gateway webhook route |
| Tool-call spans not nested in master trace | A2A hops break the OTel parent | Propagate `traceparent` inside A2A task metadata |

---

## Setup

### Prerequisites

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) package manager
- OpenAI API key (or an Ollama instance)
- Tavily API key (for the company expert's web search)

### Install

```bash
git clone https://github.com/chandra447/remote-a2a-poc.git
cd remote-a2a-poc
uv sync
```

### Environment variables

Create a `.env` file in the project root:

```env
# LLM — pick one provider
OPENAI_API_KEY=sk-...
# LLM_PROVIDER=ollama
# OLLAMA_HOST=http://localhost:11434
# LLM_MODEL=llama3.2

# Tavily web search (for company_expert)
TAVILY_API_KEY=tvly-...

# Company expert server
AGENT_HOST=0.0.0.0
AGENT_PORT=8001

# Master agent peers (comma-separated A2A server URLs)
A2A_PEER_URLS=http://localhost:8001
```

---

## Running the POC

### 1. Start the Company Expert (specialist A2A server)

```bash
uv run python main.py
# Server starts on http://localhost:8001
# AgentCard: http://localhost:8001/.well-known/agent-card.json
```

### 2. Start the Master Agent (LangGraph Studio)

```bash
uv run --group dev langgraph dev
# Studio opens at http://localhost:2024
```

Open the LangGraph Studio URL in your browser and send a question like:

> *"What are Apple's latest quarterly earnings and how does its revenue growth compare to Microsoft?"*

The master agent will discover the company expert peer, delegate the question,
and synthesise the response.

### 3. Optional — view traces with Motel

```bash
# Install motel (local OTLP ingest)
brew install motel   # or: cargo install motel

motel serve
# Traces visible at http://localhost:7777
```

Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in your `.env` to
enable tracing.

---

## Project Structure

```
.
├── main.py                          # Company Expert A2A server entry point
├── langgraph.json                   # LangGraph dev config (master agent)
├── pyproject.toml
├── src/
│   ├── company_expert/
│   │   ├── agent.py                 # LangChain agent + Tavily tool
│   │   ├── executor.py              # A2A AgentExecutor adapter
│   │   ├── settings.py              # Pydantic settings (env vars)
│   │   └── tracing.py               # OpenTelemetry setup
│   └── master_agent/
│       ├── agent.py                 # LangGraph create_agent + peer wiring
│       ├── a2a_client.py            # RemoteA2AAgent (A2A JSON-RPC client)
│       ├── tools.py                 # build_peer_tools() — card → LangChain tool
│       ├── settings.py              # Pydantic settings (env vars)
│       └── __main__.py              # CLI entry point
└── docs/
    └── a2a-async-orchestration.md   # Production async design doc
```

---

## Production Path

This POC uses synchronous, blocking A2A calls. For production with long-running
specialists deployed across AWS landing zones, see the detailed design in
[`docs/a2a-async-orchestration.md`](docs/a2a-async-orchestration.md), which covers:

- A2A `blocking=false` + push-notification webhooks
- LangGraph `interrupt()` / `Command(resume=...)` for durable async suspension
- DynamoDB correlation store (`task_id → thread_id`)
- API Gateway → SQS webhook ingress (zero-compute ingest, SigV4/IAM auth)
- Multi-instance ECS with shared Postgres checkpointer
- OpenTelemetry trace continuity across landing zones
- Idempotency, deadlines, and cancellation

---

## Dependencies

| Package | Purpose |
|---|---|
| `a2a-sdk` | A2A Protocol client + server (JSON-RPC, task lifecycle) |
| `langgraph` | Agent graph runtime, checkpointing, streaming |
| `langchain` | `create_agent`, tool abstractions, model init |
| `langchain-openai` / `langchain-ollama` | LLM providers |
| `langchain-tavily` | Web search tool for company expert |
| `opentelemetry-*` | Distributed tracing |
| `fastapi` + `uvicorn` | HTTP server for A2A endpoints |
| `pydantic-settings` | Env-var based configuration |
