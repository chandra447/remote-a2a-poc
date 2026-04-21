from pydantic import Field
from pydantic_settings import BaseSettings


class MasterSettings(BaseSettings):
    llm_model: str = Field(description="Model name to use", alias="MODEL_NAME")
    llm_provider: str = Field(
        default="ollama", description="Model provider", alias="MODEL_PROVIDER"
    )
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    ollama_host: str | None = Field(default=None, alias="OLLAMA_HOST")

    a2a_peer_urls: str = Field(
        default="http://localhost:8000",
        description="Comma-separated base URLs of A2A peer agents to discover.",
        alias="A2A_PEER_URLS",
    )
    a2a_request_timeout_s: float = Field(
        default=120.0,
        description="Per-request timeout when calling remote A2A agents",
        alias="A2A_REQUEST_TIMEOUT_S",
    )

    checkpoint_db_path: str = Field(
        default=".data/master_agent_checkpoints.sqlite",
        description="Path to the SQLite file backing the LangGraph checkpointer",
        alias="CHECKPOINT_DB_PATH",
    )

    @property
    def peer_urls(self) -> list[str]:
        return [u.strip() for u in self.a2a_peer_urls.split(",") if u.strip()]
