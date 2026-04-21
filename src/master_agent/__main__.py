from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from master_agent.agent import build_master_agent_async
from master_agent.settings import MasterSettings
from master_agent.tools import build_peer_tools


async def run_repl(thread_id: str) -> None:
    load_dotenv()
    settings = MasterSettings()

    db_path = Path(settings.checkpoint_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print("Master agent ready.")
    print(f"  thread_id      : {thread_id}")
    print(f"  a2a peers      : {settings.peer_urls}")
    print(f"  checkpoint db  : {db_path}")
    print("Type 'exit' or Ctrl-D to quit.\n")

    async with AsyncExitStack() as stack:
        peer_tools = await build_peer_tools(
            settings.peer_urls, request_timeout_s=settings.a2a_request_timeout_s
        )
        for pt in peer_tools:
            stack.push_async_callback(pt.remote.aclose)

        if not peer_tools:
            print("(no reachable A2A peers; master agent will answer directly)\n")
        else:
            for pt in peer_tools:
                card = pt.remote.card
                assert card is not None
                print(
                    f"  discovered -> {card.name} v{card.version}  "
                    f"[tool: {pt.tool.name}]"
                )
            print()

        checkpointer = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(db_path))
        )
        agent, _ = await build_master_agent_async(
            settings, checkpointer=checkpointer, peer_tools=peer_tools
        )
        config = {"configurable": {"thread_id": thread_id}}

        while True:
            try:
                user_input = await asyncio.to_thread(input, "you > ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break

            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=user_input)]},
                config=config,
            )
            reply = result["messages"][-1].content
            print(f"\nagent > {reply}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Master agent REPL")
    parser.add_argument(
        "--thread-id",
        default=os.environ.get("MASTER_THREAD_ID") or str(uuid.uuid4()),
        help="Checkpoint thread id; reuse across runs to resume a conversation.",
    )
    args = parser.parse_args()
    asyncio.run(run_repl(args.thread_id))


if __name__ == "__main__":
    main()
