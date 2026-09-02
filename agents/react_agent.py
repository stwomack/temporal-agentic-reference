"""Shared machinery for the tool-calling specialist agents.

Every specialist is a small ReAct loop:

    call_model  ->  run_tools  ->  call_model  ->  ...  ->  END

The loop ends when the model calls the agent's terminal `submit_finding` tool
instead of a data tool. Binding the finding schema as a tool rather than making
a separate structured-output call keeps the agent at one Bedrock call per turn
with no extra round trip just to format the answer.

Both nodes run as Temporal Activities, so every model call and every tool call
gets its own timeout and retry policy and lands in workflow history.

A note on the shape of this module. The Temporal LangGraph plugin identifies
each node by its module and qualified name, so node functions must be defined
at module level; a closure returned from a factory raises "closures/local
functions are not supported". That is why the logic lives here as
`run_model_turn` and `run_tool_calls` taking an `AgentSpec`, while each agent
module declares its own two-line `call_model` and `run_tools` at module level.
It also gives every agent a distinct activity name in the Temporal UI, which is
worth having when five agents are running.

Graph state is deliberately plain JSON, because it crosses the Activity
boundary on every hop. Messages travel as dicts via LangChain's
messages_to_dict, which preserves message subclasses and tool call ids.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Sequence

from botocore.exceptions import ClientError
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from typing_extensions import TypedDict

from common.bedrock import build_chat_model
from common.config import get_settings

logger = logging.getLogger(__name__)

# A tool-using agent that has not concluded after this many model turns is
# looping. Fail loudly rather than burning tokens.
MAX_TURNS = 6

# Permanent Bedrock failures. Retrying these just hides a misconfiguration.
NON_RETRYABLE_BEDROCK_CODES = {
    "AccessDeniedException",
    "ValidationException",
    "ResourceNotFoundException",
    "UnrecognizedClientException",
}


class AgentState(TypedDict, total=False):
    """Graph state. JSON only, because it crosses the Activity boundary."""

    task: str
    messages: list[dict[str, Any]]
    finding: dict[str, Any]
    turns: int
    tool_calls: list[str]
    model_id: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class AgentSpec:
    """Everything that distinguishes one specialist from another."""

    name: str
    system_prompt: str
    finding_schema: type[BaseModel]
    tools: Sequence[BaseTool] = field(default_factory=tuple)
    model_timeout: timedelta = timedelta(seconds=120)
    tool_timeout: timedelta = timedelta(seconds=30)
    max_attempts: int = 3

    @property
    def model_id(self) -> str:
        return get_settings().model_for(self.name)

    @property
    def tools_by_name(self) -> dict[str, BaseTool]:
        return {tool.name: tool for tool in self.tools}


def _bedrock_error(exc: ClientError) -> ApplicationError:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    message = exc.response.get("Error", {}).get("Message", str(exc))
    return ApplicationError(
        f"Bedrock {code}: {message}",
        type=code,
        non_retryable=code in NON_RETRYABLE_BEDROCK_CODES,
    )


def run_model_turn(spec: AgentSpec, state: AgentState) -> dict[str, Any]:
    """One Bedrock turn for the given agent. Called from an Activity."""
    model_id = spec.model_id
    turns = int(state.get("turns", 0)) + 1
    if turns > MAX_TURNS:
        raise ApplicationError(
            f"Agent {spec.name} exceeded {MAX_TURNS} turns without submitting a "
            f"finding. Check the system prompt and the tool descriptions.",
            non_retryable=True,
        )

    history = state.get("messages") or []
    if history:
        messages = messages_from_dict(history)
    else:
        messages = [
            SystemMessage(content=spec.system_prompt),
            HumanMessage(content=state["task"]),
        ]

    # On the last permitted turn, take the data tools away and offer only the
    # finding schema. The model then has nothing to call except its conclusion,
    # which converts a would-be infinite tool loop into an answer. Without this
    # an agent occasionally keeps re-querying the same tool until it trips
    # MAX_TURNS and fails the whole workflow, which is not something to
    # discover during a live demo.
    final_turn = turns >= MAX_TURNS - 1
    bindable = [spec.finding_schema] if final_turn else [*spec.tools, spec.finding_schema]
    if final_turn and spec.tools:
        messages.append(
            HumanMessage(
                content=(
                    "You have gathered enough. Call "
                    f"{spec.finding_schema.__name__} now with your conclusion "
                    "based on what you already have."
                )
            )
        )

    logger.info(
        "agent %s turn %d: model_id=%s tools=%d%s",
        spec.name,
        turns,
        model_id,
        len(bindable) - 1,
        " (final turn, must conclude)" if final_turn else "",
    )

    model = build_chat_model(model_id=model_id).bind_tools(bindable)
    try:
        response = model.invoke(messages)
    except ClientError as exc:
        raise _bedrock_error(exc) from exc

    usage = getattr(response, "usage_metadata", None) or {}
    messages.append(response)

    finding: dict[str, Any] = {}
    requested: list[str] = []
    for call in response.tool_calls or []:
        if call["name"] == spec.finding_schema.__name__:
            # Validate through the schema so a malformed finding fails here
            # rather than downstream in the workflow.
            finding = json.loads(spec.finding_schema(**call["args"]).model_dump_json())
        else:
            requested.append(call["name"])

    if not finding and not requested:
        # The model answered in prose instead of calling anything. Say so
        # explicitly, otherwise the next turn sees the same input and produces
        # the same non-answer.
        messages.append(
            HumanMessage(
                content=(
                    f"Respond by calling the {spec.finding_schema.__name__} tool. "
                    "Do not answer in plain text."
                )
            )
        )

    logger.info(
        "agent %s turn %d complete: tool_calls=%s submitted=%s tokens=%din/%dout",
        spec.name,
        turns,
        requested or "none",
        bool(finding),
        usage.get("input_tokens", 0),
        usage.get("output_tokens", 0),
    )

    return {
        "messages": messages_to_dict(messages),
        "turns": turns,
        "finding": finding,
        "model_id": model_id,
        "input_tokens": int(state.get("input_tokens", 0))
        + int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(state.get("output_tokens", 0))
        + int(usage.get("output_tokens", 0) or 0),
    }


def run_tool_calls(spec: AgentSpec, state: AgentState) -> dict[str, Any]:
    """Execute whatever the model asked for. Called from an Activity."""
    messages = messages_from_dict(state["messages"])
    last = messages[-1]
    executed = list(state.get("tool_calls") or [])
    by_name = spec.tools_by_name

    for call in getattr(last, "tool_calls", None) or []:
        if call["name"] == spec.finding_schema.__name__:
            continue
        selected = by_name.get(call["name"])
        if selected is None:
            content = json.dumps(
                {"error": f"No such tool: {call['name']}", "available": list(by_name)}
            )
        else:
            try:
                content = selected.invoke(call["args"])
            except Exception as exc:  # noqa: BLE001
                # Hand the failure back to the model as a tool result. It can
                # correct a bad argument itself, which is cheaper than a
                # workflow level retry of the whole agent.
                content = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        logger.info("agent %s tool %s -> %d chars", spec.name, call["name"], len(content))
        executed.append(call["name"])
        messages.append(
            ToolMessage(content=content, tool_call_id=call["id"], name=call["name"])
        )

    return {"messages": messages_to_dict(messages), "tool_calls": executed}


def decide_next(spec: AgentSpec, state: AgentState) -> str:
    """Loop again, run tools, or stop. Runs on the workflow thread."""
    if state.get("finding"):
        return END
    messages = messages_from_dict(state["messages"])
    pending = [
        call
        for call in getattr(messages[-1], "tool_calls", None) or []
        if call["name"] != spec.finding_schema.__name__
    ]
    return "run_tools" if pending else "call_model"


def assemble_graph(
    spec: AgentSpec,
    call_model: Callable[[AgentState], dict[str, Any]],
    run_tools: Callable[[AgentState], dict[str, Any]] | None = None,
) -> StateGraph:
    """Wire one agent's module level nodes into a graph.

    The node metadata is what the Temporal LangGraph plugin reads to decide
    where each node runs and with what timeout and retry policy.
    """
    graph = StateGraph(AgentState)
    graph.add_node(
        "call_model",
        call_model,
        metadata={
            "execute_in": "activity",
            "start_to_close_timeout": spec.model_timeout,
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=20),
                maximum_attempts=spec.max_attempts,
            ),
            "summary": f"{spec.name}: Bedrock turn",
        },
    )
    graph.add_edge(START, "call_model")

    # The router must be async. LangGraph awaits an async branch function
    # directly, but offloads a sync one through loop.run_in_executor, and a
    # Temporal workflow's event loop raises NotImplementedError for that on
    # purpose: running workflow logic on a thread pool would be
    # non-deterministic. A sync router here fails the workflow task with a bare
    # NotImplementedError from asyncio/events.py.
    async def router(state: AgentState) -> str:
        return decide_next(spec, state)

    if spec.tools and run_tools is not None:
        graph.add_node(
            "run_tools",
            run_tools,
            metadata={
                "execute_in": "activity",
                "start_to_close_timeout": spec.tool_timeout,
                "retry_policy": RetryPolicy(maximum_attempts=3),
                "summary": f"{spec.name}: tool calls",
            },
        )
        graph.add_edge("run_tools", "call_model")
        graph.add_conditional_edges(
            "call_model",
            router,
            {"run_tools": "run_tools", "call_model": "call_model", END: END},
        )
    else:
        # No tools, so the agent must submit a finding on its single turn.
        graph.add_conditional_edges(
            "call_model", router, {"call_model": "call_model", END: END}
        )

    return graph
