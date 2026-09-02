"""Temporal worker.

Run it with:

    uv run python worker.py

Hosts the orchestrator workflow, the two plain activities, and the activities
the LangGraph plugin generates for the extraction graph's nodes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from common.preflight import require_dependencies

require_dependencies()

from temporalio.worker import Worker  # noqa: E402

from activities.erp import submit_to_erp  # noqa: E402
from activities.guardrail import check_guardrails  # noqa: E402
from common.config import get_settings  # noqa: E402
from common.temporal_client import build_workflow_runner, connect  # noqa: E402
from workflows.po_approval import POApprovalWorkflow  # noqa: E402

logger = logging.getLogger("worker")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    client = await connect()

    logger.info(
        "worker starting: address=%s namespace=%s task_queue=%s bedrock_model=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.temporal_task_queue,
        settings.bedrock_model_id,
    )

    # check_guardrails and submit_to_erp are sync activities, so they need a
    # thread pool. The LangGraph node activities are async and run on the loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as activity_executor:
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[POApprovalWorkflow],
            activities=[check_guardrails, submit_to_erp],
            activity_executor=activity_executor,
            workflow_runner=build_workflow_runner(),
            # No plugins= here on purpose. Since Python SDK 1.32 the Worker
            # inherits the Client's plugins, so passing the LangGraph plugin to
            # both registers its node activities twice and the Worker refuses
            # to start with "More than one activity named
            # po-extraction.extract_po".
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
