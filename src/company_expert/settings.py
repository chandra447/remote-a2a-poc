from pydantic_settings import BaseSettings
from pydantic import Field
from dotenv import load_dotenv

_ = load_dotenv()


class AppSettings(BaseSettings):
    tavily_api_key: str = Field(description="Tavily API key", alias="TAVILY_API_KEY")
    llm_model: str = Field(description="Model name to use", alias="MODEL_NAME")
    llm_provider: str = Field(default="ollama", description="Model provider", alias="MODEL_PROVIDER")
    # For Ollama: set OLLAMA_HOST (e.g. http://localhost:11435) — picked up by ollama client automatically
    # For OpenAI-compatible providers: set OPENAI_BASE_URL (e.g. http://localhost:11435/v1)
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    ollama_host: str | None = Field(default=None, alias="OLLAMA_HOST")
    host: str = Field(default="0.0.0.0", description="Server host", alias="HOST")
    port: int = Field(default=8001, description="Server port", alias="PORT")
    agent_url: str = Field(default="http://localhost:8001/", description="Public URL of this agent", alias="AGENT_URL")
    artificial_delay_s: float = Field(
        default=300.0,
        description="Artificial delay in seconds before doing real work (simulates long-running specialist)",
        alias="ARTIFICIAL_DELAY_S",
    )

