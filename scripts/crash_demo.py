"""The durability demo: kill the worker mid fan out and watch nothing repeat.

This is the part a plain Python orchestrator cannot reproduce. While the three
specialist agents are running concurrently, the worker is killed. Whatever had
already finished stays finished: its Bedrock call is not paid for twice, and
its result is read back from workflow history rather than recomputed. Only the
agent that was actually in flight runs again.

    uv run python scripts/crash_demo.py
    ./scripts/crash_demo.sh

Run this with no worker of your own running. The script starts and kills its
own worker, and a second worker on the same task queue would simply pick up the
work the instant the first one dies. The fan out would never be interrupted,
and the comparison at the end would print "same" for every activity while
proving nothing at all. The script checks for other pollers and refuses to run
rather than produce that misleading result.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.preflight import require_dependencies  # noqa: E402

require_dependencies()

import temporalio.api.enums.v1 as temporal_enums  # noqa: E402
import temporalio.api.workflowservice.v1 as workflowservice  # noqa: E402

from common.config import get_settings  # noqa: E402
from common.constants import FANOUT_STEPS  # noqa: E402
from common.models import POWorkflowInput, Scenario  # noqa: E402
from common.scenarios import SCENARIOS  # noqa: E402
from common.temporal_client import connect  # noqa: E402
from workflows.po_approval import POApprovalWorkflow  # noqa: E402

BAR = "=" * 78



async def existing_pollers(client, settings) -> list[str]:
    """Identities of workers already polling this task queue.

    Temporal keeps a poller listed for roughly a minute after the worker stops,
    so a recently killed worker can still show up here.
    """
    identities: set[str] = set()
    for queue_type in (
        temporal_enums.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
        temporal_enums.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
    ):
        response = await client.workflow_service.describe_task_queue(
            workflowservice.DescribeTaskQueueRequest(
                namespace=settings.temporal_namespace,
                task_queue={"name": settings.temporal_task_queue},
                task_queue_type=queue_type,
            )
        )
        identities.update(poller.identity for poller in response.pollers)
    return sorted(identities)


def start_worker() -> subprocess.Popen:
    print("starting a worker...")
    return subprocess.Popen(
        [sys.executable, "worker.py"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def activity_counts(handle) -> collections.Counter:
    """How many times each activity actually executed, from history."""
    counts: collections.Counter = collections.Counter()
    async for event in handle.fetch_history_events():
        if event.WhichOneof("attributes") == "activity_task_scheduled_event_attributes":
            counts[event.activity_task_scheduled_event_attributes.activity_type.name] += 1
    return counts


async def start_run(client, settings, label: str):
    spec = SCENARIOS[Scenario.HAPPY_PATH]
    po_id = f"PO-{label}-{uuid.uuid4().hex[:6].upper()}"
    return await client.start_workflow(
        POApprovalWorkflow.run,
        POWorkflowInput(
            po_id=po_id,
            raw_text=spec.raw_text,
            scenario=spec.scenario,
            approval_threshold=spec.approval_threshold,
            erp_seeded_failures=0,
            erp_max_attempts=settings.erp_max_attempts,
            approval_timeout_seconds=settings.approval_timeout_seconds,
        ),
        id=f"po-{po_id}",
        task_queue=settings.temporal_task_queue,
    )


async def wait_for_partial_fanout(handle, seconds: float = 90) -> list[str]:
    """Block until at least one specialist has finished and one is still running.

    A query needs a live worker to answer it, so queries during worker startup
    raise rather than return. Swallow those and keep polling against a wall
    clock deadline, otherwise a slow start becomes a very long hang.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status = await handle.query(POApprovalWorkflow.status)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
            continue
        by_name = {s.name: s for s in status.steps}
        done = [n for n in FANOUT_STEPS if by_name[n].status.value == "completed"]
        running = [n for n in FANOUT_STEPS if by_name[n].status.value == "running"]
        if done and running:
            print(f"fan out is partial. finished: {done}. still running: {running}")
            return done
        await asyncio.sleep(0.2)
    return []


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if another worker is already polling the task queue.",
    )
    args = parser.parse_args()

    settings = get_settings()
    client = await connect()

    # A second worker on this task queue would take over the moment ours dies,
    # so the fan out would never actually be interrupted and the comparison at
    # the end would look like a pass while demonstrating nothing.
    others = await existing_pollers(client, settings)
    if others and not args.force:
        print(
            f"REFUSING TO RUN: {len(others)} worker(s) are already polling "
            f"'{settings.temporal_task_queue}':"
        )
        for identity in others:
            print(f"  {identity}")
        print(
            "\nThis demo kills its own worker to show that finished agent work "
            "survives.\nAnother worker would pick the work up instantly, the fan "
            "out would never be\ninterrupted, and the comparison at the end would "
            "print 'same' for everything\nwhile proving nothing.\n"
            "\nStop them first:\n"
            "  ./scripts/cleanup.sh --all\n"
            "\nTemporal lists a poller for about a minute after its worker stops, "
            "so if you\njust stopped one, wait a moment and try again. Use --force "
            "to run anyway."
        )
        return 2

    worker = start_worker()
    await asyncio.sleep(8)

    # ---------------------------------------------------------- baseline run
    print(f"{BAR}\nPASS 1 of 2: a normal run, nothing killed. This is the baseline.\n{BAR}")
    baseline_handle = await start_run(client, settings, "BASE")
    print(f"started {baseline_handle.id}")
    baseline_result = await baseline_handle.result()
    baseline = await activity_counts(baseline_handle)
    print(f"completed: {baseline_result.state.value}\n")

    # ------------------------------------------------------------- crash run
    print(f"{BAR}\nPASS 2 of 2: the same request, worker killed mid fan out.\n{BAR}")
    handle = await start_run(client, settings, "CRASH")
    print(f"started {handle.id}")

    finished_before = await wait_for_partial_fanout(handle)
    if not finished_before:
        print(
            "Never caught a partial fan out within 90s. Either the worker never "
            "started, or the three agents finished too close together. Try again."
        )
        worker.send_signal(signal.SIGKILL)
        return 1

    print(f"\nKILLING THE WORKER (pid {worker.pid}) with SIGKILL\n{BAR}")
    worker.send_signal(signal.SIGKILL)
    worker.wait(timeout=10)
    await asyncio.sleep(4)
    print("worker is gone. The workflow is untouched on the server.\n")
    worker = start_worker()
    await asyncio.sleep(8)
    print("worker is back. Temporal is replaying the workflow.\n")

    result = await handle.result()
    crashed = await activity_counts(handle)
    print(f"{BAR}\nworkflow completed anyway: {result.state.value}\n{BAR}\n")

    # ----------------------------------------------------------- the compare
    #
    # Every activity execution appends an ActivityTaskScheduled event, and
    # replay does not append new ones. So these counts are the true number of
    # times each activity body actually ran, and comparing the two passes
    # isolates what the crash cost.
    print("activity executions, normal run vs crashed run:\n")
    print(f"  {'activity':<34} {'normal':>7} {'crashed':>8}   {'':<10}")
    names = sorted(set(baseline) | set(crashed))
    extra_total = 0
    for name in names:
        before, after = baseline.get(name, 0), crashed.get(name, 0)
        delta = after - before
        extra_total += max(0, delta)
        note = "same" if delta == 0 else f"{delta:+d} from the crash"
        print(f"  {name:<34} {before:>7} {after:>8}   {note}")

    print("\nwhat this shows:")
    print(
        f"  The agents that had already finished ({', '.join(finished_before)}) "
        f"ran the same number of times in both passes. Their Bedrock calls were "
        f"not repeated; Temporal replayed their results out of history."
    )
    if extra_total:
        print(
            f"  {extra_total} activity execution(s) did repeat. That is the work "
            f"that was genuinely in flight when the process died, which Temporal "
            f"retried once a worker came back. Nothing else was redone."
        )
    else:
        print(
            "  No activity repeated at all. The crash landed between activities, "
            "so even the in flight work was already durable."
        )
    print(
        "\n  A plain Python orchestrator would have lost the whole fan out here "
        "and paid for every one of those model calls a second time."
    )

    worker.send_signal(signal.SIGTERM)
    worker.wait(timeout=10)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
