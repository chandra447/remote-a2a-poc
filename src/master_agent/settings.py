from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv()


class MasterSettings(BaseSettings):
    llm_model: str = Field(description="Model name to use", alias="MODEL_NAME")
    llm_provider: str = Field(
        default="ollama", description="Model provider", alias="MODEL_PROVIDER"
    )
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    ollama_host: str | None = Field(default=None, alias="OLLAMA_HOST")

    a2a_peer_urls: str = Field(
        default="http://localhost:8001",
        description="Comma-separated base URLs of A2A peer agents to discover.",
        alias="A2A_PEER_URLS",
    )
    a2a_request_timeout_s: float = Field(
        default=30.0,
        description="Timeout for the initial non-blocking A2A request (should complete in <1s)",
        alias="A2A_REQUEST_TIMEOUT_S",
    )

    checkpoint_db_path: str = Field(
        default=".data/master_agent_checkpoints.sqlite",
        description="Path to the SQLite file backing the LangGraph checkpointer",
        alias="CHECKPOINT_DB_PATH",
    )

    master_host: str = Field(default="0.0.0.0", alias="MASTER_HOST")
    master_port: int = Field(default=8000, alias="MASTER_PORT")
    webhook_url: str = Field(
        default="http://localhost:8000/webhook/a2a",
        description="Public URL where specialists POST completed-task callbacks",
        alias="WEBHOOK_URL",
    )

    @property
    def peer_urls(self) -> list[str]:
        return [u.strip() for u in self.a2a_peer_urls.split(",") if u.strip()]
