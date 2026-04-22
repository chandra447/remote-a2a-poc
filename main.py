import httpx
import uvicorn
from contextlib import asynccontextmanager

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.events import InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.base_push_notification_sender import BasePushNotificationSender
from a2a.server.tasks.inmemory_push_notification_config_store import (
    InMemoryPushNotificationConfigStore,
)
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from company_expert.executor import CompanyExpertExecutor
from company_expert.settings import AppSettings

settings = AppSettings()

agent_card = AgentCard(
    name="Company Expert",
    description=(
        "A financial research agent that provides accurate, data-driven insights about "
        "companies — covering financials, competitive positioning, growth strategy, and risk."
    ),
    url=settings.agent_url,
    version="1.0.0",
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=False, push_notifications=True),
    skills=[
        AgentSkill(
            id="company_financial_research",
            name="Company Financial Research",
            tags=["finance", "research", "company"],
            description=(
                "Research a company's financial health, revenue trends, profitability, "
                "market position, and key business metrics using live web search."
            ),
            examples=[
                "What are Apple's latest quarterly earnings?",
                "How does Tesla's revenue growth compare to its peers?",
                "What are the key risks facing Microsoft in 2025?",
                "Give me an overview of Nvidia's business model and competitive moat.",
            ],
        ),
        AgentSkill(
            id="competitive_analysis",
            name="Competitive Analysis",
            tags=["finance", "competitive", "analysis"],
            description=(
                "Compare companies within an industry across financial and operational KPIs, "
                "market share, and strategic positioning."
            ),
            examples=[
                "Compare AWS, Azure, and GCP market share and growth rates.",
                "Which EV companies have the strongest balance sheets right now?",
            ],
        ),
    ],
)


def _build_app() -> object:
    push_config_store = InMemoryPushNotificationConfigStore()
    # Shared httpx client for outbound push-notification POSTs.
    # Closed on shutdown via the lifespan below.
    _http = httpx.AsyncClient()
    push_sender = BasePushNotificationSender(_http, push_config_store)

    handler = DefaultRequestHandler(
        agent_executor=CompanyExpertExecutor(),
        task_store=InMemoryTaskStore(),
        queue_manager=InMemoryQueueManager(),
        push_config_store=push_config_store,
        push_sender=push_sender,
    )

    built_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=handler,
    ).build()

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await _http.aclose()

    built_app.router.lifespan_context = lifespan
    return built_app


app = _build_app()

if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
