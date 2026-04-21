import asyncio
import logging
import uuid

from langchain_core.messages import HumanMessage

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Message,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    UnsupportedOperationError,
)

from company_expert.agent import finance_agent
from company_expert.settings import AppSettings

logger = logging.getLogger(__name__)
_settings = AppSettings()


class CompanyExpertExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_input = context.get_user_input()

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.working),
                final=False,
            )
        )

        delay = _settings.artificial_delay_s
        if delay > 0:
            logger.info(
                "task %s: sleeping %.0fs to simulate long-running work",
                context.task_id,
                delay,
            )
            await asyncio.sleep(delay)

        result = await finance_agent.ainvoke(
            {"messages": [HumanMessage(content=user_input)]}
        )
        output: str = result["messages"][-1].content

        await event_queue.enqueue_event(
            Message(
                message_id=str(uuid.uuid4()),
                role=Role.agent,
                parts=[Part(root=TextPart(text=output))],
                task_id=context.task_id,
                context_id=context.context_id,
            )
        )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.completed),
                final=True,
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()
