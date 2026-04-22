from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

load_dotenv()

from master_agent.server import app  # noqa: E402 — load_dotenv must run first
from master_agent.settings import MasterSettings


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO)
    settings = MasterSettings()
    uvicorn.run(
        app,
        host=settings.master_host,
        port=settings.master_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
