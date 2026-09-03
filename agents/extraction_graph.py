"""Story 2.2: the purchase order extraction agent.

A LangGraph `StateGraph` with two nodes:

  extract_po  execute_in="activity"  a live Amazon Bedrock call, so it gets
                                     Temporal timeouts and retries
  normalize   execute_in="workflow"  deterministic cleanup, cheap enough to run
                                     on the workflow thread and replay-safe

The graph is registered with `LangGraphPlugin` in worker.py and invoked from the
orchestrator workflow via `temporalio.contrib.langgraph.graph("po-extraction")`.
The plugin turns the `extract_po` node into a real Temporal Activity, so the
Bedrock call shows up in workflow history as ActivityTaskScheduled /
ActivityTaskCompleted rather than as an opaque function call.

The Bedrock call here is always live. There is no mock, no cached response, and
no offline branch.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from botocore.exceptions import ClientError
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from typing_extensions import TypedDict

from common.bedrock import build_chat_model
from common.config import get_settings
from common.models import ExtractedPO, LineItem

logger = logging.getLogger(__name__)

GRAPH_NAME = "po-extraction"

SYSTEM_PROMPT = """You extract structured purchase order data from free-form \
procurement requests.

Rules:
- Return only fields you can support from the text. Do not invent a vendor or a \
requester.
- total_amount is the order total as a number, with no currency symbol and no \
thousands separators.
- If the text lists line items, return one entry per item with its own amount.
- If the text states a total that differs from the sum of the line items, keep \
the stated total and mention the discrepancy in notes.
- currency is a three letter ISO code. Default to USD when unstated."""


class POLineItemSchema(BaseModel):
    """Line item shape requested from the model.

    Note: these schema class names are sent to Bedrock as tool names, so they
    must not start with an underscore. Bedrock strips the leading underscore and
    the response then fails to match the registered tool.
    """

    description: str = Field(description="What is being purchased")
    quantity: float = Field(default=1.0, description="Units ordered")
    unit_price: float = Field(default=0.0, description="Price per unit")
    amount: float = Field(default=0.0, description="Extended amount for this line")


class POExtractionSchema(BaseModel):
    """The structured output contract handed to Bedrock."""

    # vendor, requester, currency and total_amount are intentionally required.
    # With defaults they are optional in the generated tool schema and models
    # routinely omit them, which shows up as blank fields in the UI.
    vendor: str = Field(
        description="Supplier the PO is issued to. Empty string if not stated."
    )
    requester: str = Field(
        description="Person or team requesting. Empty string if not stated."
    )
    currency: str = Field(description="Three letter ISO currency code, e.g. USD")
    total_amount: float = Field(description="Order total as a number")
    line_items: list[POLineItemSchema] = Field(
        default_factory=list, description="One entry per line item in the request"
    )
    notes: str = Field(default="", description="Anything unusual worth flagging")


class ExtractionState(TypedDict, total=False):
    """Graph state. Kept to JSON-friendly values because it crosses the
    Activity boundary on every node hop."""

    raw_text: str
    extracted: dict[str, Any]


def extract_po(state: ExtractionState) -> dict[str, Any]:
    """Live Bedrock call. Runs as a Temporal Activity."""
    settings = get_settings()
    raw_text = state.get("raw_text", "")
    if not raw_text.strip():
        # A blank request will never succeed on retry, so fail permanently.
        raise ApplicationError(
            "raw_text is empty, nothing to extract", non_retryable=True
        )

    logger.info(
        "bedrock extraction starting: model_id=%s region=%s chars=%d",
        settings.bedrock_model_id,
        settings.bedrock_region,
        len(raw_text),
    )

    model = build_chat_model()
    structured = model.with_structured_output(POExtractionSchema, include_raw=True)

    try:
        response = structured.invoke(
            [("system", SYSTEM_PROMPT), ("human", raw_text)]
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        # Access and validation problems are configuration bugs: retrying just
        # burns the activity's retry budget and hides the cause.
        if code in {
            "AccessDeniedException",
            "ValidationException",
            "ResourceNotFoundException",
            "UnrecognizedClientException",
        }:
            raise ApplicationError(
                f"Bedrock {code}: {message}", type=code, non_retryable=True
            ) from exc
        # Throttling and 5xx are transient. Let Temporal back off and retry.
        raise ApplicationError(f"Bedrock {code}: {message}", type=code) from exc

    parsed: POExtractionSchema | None = response.get("parsed")
    raw_message = response.get("raw")
    if parsed is None:
        detail = response.get("parsing_error") or "model returned no structured output"
        raise ApplicationError(f"Extraction produced no structured output: {detail}")

    usage = getattr(raw_message, "usage_metadata", None) or {}
    result = ExtractedPO(
        vendor=parsed.vendor,
        requester=parsed.requester,
        currency=parsed.currency,
        total_amount=parsed.total_amount,
        line_items=[LineItem(**item.model_dump()) for item in parsed.line_items],
        notes=parsed.notes,
        model_id=settings.bedrock_model_id,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
    )

    logger.info(
        "bedrock extraction complete: vendor=%r total=%s tokens_in=%d tokens_out=%d",
        result.vendor,
        result.total_amount,
        result.input_tokens,
        result.output_tokens,
    )
    return {"extracted": json.loads(result.model_dump_json())}


def normalize(state: ExtractionState) -> dict[str, Any]:
    """Deterministic cleanup. Runs on the workflow thread, not in an Activity."""
    extracted = ExtractedPO.model_validate(state.get("extracted") or {})

    extracted.currency = (extracted.currency or "USD").strip().upper()[:3] or "USD"
    extracted.vendor = extracted.vendor.strip()
    extracted.requester = extracted.requester.strip()

    # Fill in a per line amount the model left blank.
    for item in extracted.line_items:
        if not item.amount and item.quantity and item.unit_price:
            item.amount = round(item.quantity * item.unit_price, 2)

    # Fall back to the sum of the lines only when no total was extracted.
    if not extracted.total_amount and extracted.line_items:
        extracted.total_amount = round(
            sum(item.amount for item in extracted.line_items), 2
        )

    extracted.total_amount = round(float(extracted.total_amount), 2)
    return {"extracted": json.loads(extracted.model_dump_json())}


def build_extraction_graph() -> StateGraph:
    """Assemble the graph. Node metadata carries the Temporal execution
    location and activity options the plugin applies."""
    settings = get_settings()

    graph = StateGraph(ExtractionState)
    graph.add_node(
        "extract_po",
        extract_po,
        metadata={
            "execute_in": "activity",
            "start_to_close_timeout": timedelta(seconds=120),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                # Same reasoning as the specialists: absorb a Bedrock throttle
                # rather than fail the run. See agents/react_agent.py.
                maximum_attempts=5,
            ),
            "summary": f"Bedrock extraction via {settings.bedrock_model_id}",
        },
    )
    graph.add_node("normalize", normalize, metadata={"execute_in": "workflow"})
    graph.add_edge(START, "extract_po")
    graph.add_edge("extract_po", "normalize")
    graph.add_edge("normalize", END)
    return graph
