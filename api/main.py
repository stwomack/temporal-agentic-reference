"""Story 3.1: thin HTTP layer between the browser UI and the Temporal client.

Deliberately thin. It holds no state of its own: every status answer comes from
the workflow's own `status` query plus DescribeWorkflowExecution, and every
decision goes in as a Temporal Update. If the workflow is the source of truth,
the UI cannot drift from it.

Errors are surfaced, never swallowed. A Temporal RPC failure becomes a 4xx or
5xx with the service's own message in the body.

Run it with:

    uv run uvicorn api.main:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.protobuf.json_format import MessageToDict
from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError, RPCStatusCode

from api.source import SourceLookupError, load_step_source
from common.config import get_settings
from common.constants import STEP_LABELS, STEP_ORDER
from common.models import (
    ApprovalDecision,
    POWorkflowInput,
    POWorkflowStatus,
    StartRequest,
    StatusResponse,
)
from common.scenarios import SCENARIOS
from common.temporal_client import TemporalConnectionError, connect, describe_target
from workflows.po_approval import POApprovalWorkflow

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = REPO_ROOT / "ui"

# How often the SSE endpoint re-reads the workflow query. This is the UI's
# refresh loop, not the human approval mechanism, which is an Update.
POLL_INTERVAL_SECONDS = 0.4

_client: Optional[Client] = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _client
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        _client = await connect()
    except TemporalConnectionError as exc:
        # A dead or misconfigured Temporal is the most common startup failure.
        # Say so once, loudly, with the advice attached, rather than failing
        # every request later.
        raise RuntimeError(str(exc)) from None
    logger.info(
        "API ready: %s task_queue=%s bedrock_model=%s",
        describe_target(settings),
        settings.temporal_task_queue,
        settings.bedrock_model_id,
    )
    yield
    _client = None


app = FastAPI(title="Durable PO approval demo", lifespan=lifespan)


def client() -> Client:
    if _client is None:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    return _client


def handle(workflow_id: str) -> WorkflowHandle:
    return client().get_workflow_handle_for(POApprovalWorkflow.run, workflow_id)


def _temporal_ui_url(workflow_id: str) -> str:
    """Deep link to this execution, on the local web UI or on Temporal Cloud."""
    settings = get_settings()
    return (
        f"{settings.temporal_ui_base_url}/namespaces/"
        f"{settings.temporal_namespace}/workflows/{workflow_id}"
    )


def _failure_chain(exc: BaseException) -> str:
    """Flatten an exception and its causes into one readable message.

    Temporal wraps a rejected Update as WorkflowUpdateFailedError whose cause
    carries the actual reason. Reporting only the outer exception yields the
    useless string "Workflow update failed".
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        if message and message not in parts:
            parts.append(message)
        current = current.__cause__ or getattr(current, "cause", None)
    return ": ".join(parts) if parts else type(exc).__name__


def _http_error(exc: Exception) -> HTTPException:
    """Map a Temporal failure onto an HTTP status without hiding the message."""
    if isinstance(exc, RPCError):
        mapping = {
            RPCStatusCode.NOT_FOUND: 404,
            RPCStatusCode.INVALID_ARGUMENT: 400,
            RPCStatusCode.ALREADY_EXISTS: 409,
            RPCStatusCode.FAILED_PRECONDITION: 409,
            RPCStatusCode.PERMISSION_DENIED: 403,
            RPCStatusCode.UNAUTHENTICATED: 401,
            RPCStatusCode.DEADLINE_EXCEEDED: 504,
            RPCStatusCode.UNAVAILABLE: 503,
        }
        return HTTPException(
            status_code=mapping.get(exc.status, 502),
            detail=f"Temporal {exc.status.name}: {exc.message}",
        )
    return HTTPException(
        status_code=500, detail=f"{type(exc).__name__}: {_failure_chain(exc)}"
    )


async def _read_status(workflow_id: str) -> StatusResponse:
    """Combine the workflow's own view with the server's view of it.

    The query gives step level detail. Describe gives the live retry state of a
    pending activity, which the workflow itself cannot see mid flight.
    """
    wf = handle(workflow_id)
    try:
        description = await wf.describe()
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    pending: list[dict[str, Any]] = []
    raw = getattr(description, "raw_description", None)
    for activity in getattr(raw, "pending_activities", []) or []:
        info = MessageToDict(activity, preserving_proto_field_name=True)
        pending.append(
            {
                "activity_type": (info.get("activity_type") or {}).get("name", ""),
                "state": info.get("state", ""),
                "attempt": int(info.get("attempt", 1) or 1),
                "maximum_attempts": int(info.get("maximum_attempts", 0) or 0),
                "last_failure": (info.get("last_failure") or {}).get("message", ""),
                "scheduled_time": info.get("scheduled_time", ""),
            }
        )

    status: Optional[POWorkflowStatus] = None
    query_error = ""
    try:
        status = await wf.query(POApprovalWorkflow.status)
    except Exception as exc:  # noqa: BLE001
        # A completed workflow can still be queried, so a failure here is real
        # and worth showing rather than hiding behind an empty panel.
        query_error = f"{type(exc).__name__}: {exc}"
        logger.warning("status query failed for %s: %s", workflow_id, query_error)

    if status is not None and query_error:
        status.error = query_error

    return StatusResponse(
        workflow_id=workflow_id,
        run_id=description.run_id,
        temporal_ui_url=_temporal_ui_url(workflow_id),
        execution_status=description.status.name if description.status else "UNKNOWN",
        pending_activities=pending,
        status=status,
    )


# --------------------------------------------------------------------- routes


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "temporal_address": settings.temporal_address,
        "temporal_namespace": settings.temporal_namespace,
        "temporal_ui_url": settings.temporal_ui_base_url,
        "temporal_tls": settings.tls_enabled,
        "task_queue": settings.temporal_task_queue,
        "bedrock_region": settings.bedrock_region,
        "bedrock_model_id": settings.bedrock_model_id,
        "approval_threshold": settings.po_approval_threshold,
        "hard_block_threshold": settings.po_hard_block_threshold,
        "blocked_vendors": settings.blocked_vendor_list,
    }


@app.get("/api/steps")
async def steps() -> list[dict[str, str]]:
    """The diagram's node list, so the UI does not hardcode the pipeline."""
    return [{"name": name, "label": STEP_LABELS[name]} for name in STEP_ORDER]


@app.get("/api/scenarios")
async def scenarios() -> list[dict[str, Any]]:
    return [spec.model_dump() for spec in SCENARIOS.values()]


@app.post("/api/workflows")
async def start_workflow(request: StartRequest) -> dict[str, Any]:
    settings = get_settings()
    spec = SCENARIOS.get(request.scenario)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"Unknown scenario {request.scenario}")

    raw_text = (request.raw_text or spec.raw_text).strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text cannot be empty")

    po_id = f"PO-{uuid.uuid4().hex[:8].upper()}"
    workflow_input = POWorkflowInput(
        po_id=po_id,
        raw_text=raw_text,
        scenario=request.scenario,
        approval_threshold=(
            request.approval_threshold
            if request.approval_threshold is not None
            else spec.approval_threshold
        ),
        erp_seeded_failures=(
            request.erp_seeded_failures
            if request.erp_seeded_failures is not None
            else spec.erp_seeded_failures
        ),
        erp_max_attempts=settings.erp_max_attempts,
        approval_timeout_seconds=settings.approval_timeout_seconds,
    )

    try:
        wf = await client().start_workflow(
            POApprovalWorkflow.run,
            workflow_input,
            id=f"po-{po_id}",
            task_queue=settings.temporal_task_queue,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    logger.info("started %s for scenario %s", wf.id, request.scenario.value)
    return {
        "workflow_id": wf.id,
        "run_id": wf.result_run_id or "",
        "po_id": po_id,
        "scenario": request.scenario.value,
        "temporal_ui_url": _temporal_ui_url(wf.id),
    }


@app.get("/api/workflows/{workflow_id}")
async def get_workflow(workflow_id: str) -> StatusResponse:
    return await _read_status(workflow_id)


@app.post("/api/workflows/{workflow_id}/decision")
async def submit_decision(
    workflow_id: str, decision: ApprovalDecision
) -> POWorkflowStatus:
    """Deliver the human decision as a Temporal Update.

    execute_update waits for the handler to finish, so a 200 here means the
    workflow has actually acted on the decision. An Update rejected by the
    validator (for example, the workflow is not waiting) comes back as a 409
    with the reason.
    """
    try:
        return await handle(workflow_id).execute_update(
            POApprovalWorkflow.submit_decision, decision
        )
    except RPCError as exc:
        raise _http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        # Update validator rejections and handler failures land here. They are
        # client errors, so 409 rather than 500.
        message = _failure_chain(exc)
        logger.warning("decision rejected for %s: %s", workflow_id, message)
        raise HTTPException(status_code=409, detail=message) from exc


@app.get("/api/workflows/{workflow_id}/stream")
async def stream_workflow(workflow_id: str, request: Request) -> StreamingResponse:
    """Server sent events carrying the full status object whenever it changes.

    This is how the diagram and the telemetry feed stay live without the
    browser polling or the user pressing refresh.
    """

    async def event_source() -> AsyncIterator[str]:
        previous: Optional[str] = None
        while True:
            if await request.is_disconnected():
                return
            try:
                payload = (await _read_status(workflow_id)).model_dump_json()
            except HTTPException as exc:
                yield f"event: error\ndata: {json.dumps({'detail': exc.detail})}\n\n"
                return
            if payload != previous:
                previous = payload
                yield f"data: {payload}\n\n"
            body = json.loads(payload)
            terminal = body.get("execution_status") not in (
                "WORKFLOW_EXECUTION_STATUS_RUNNING",
                "RUNNING",
            )
            if terminal:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/source/{step}")
async def step_source(step: str) -> dict[str, Any]:
    """Story 4.1: the code the given step runs, with the line range to highlight."""
    try:
        return load_step_source(step)
    except SourceLookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
