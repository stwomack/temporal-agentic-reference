"""Temporal client and plugin wiring, shared by the worker and the API.

Both processes must build the same `LangGraphPlugin` instance shape and use the
same data converter, otherwise the workflow input and the graph state will not
round trip.
"""

from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from agents.extraction_graph import build_extraction_graph
from common.config import get_settings
from common.constants import EXTRACTION_GRAPH_NAME


def build_langgraph_plugin() -> LangGraphPlugin:
    """The LangGraph plugin, in Public Preview in the Temporal Python SDK.

    It registers the extraction graph and turns every node marked
    execute_in="activity" into a Temporal Activity.
    """
    return LangGraphPlugin(graphs={EXTRACTION_GRAPH_NAME: build_extraction_graph()})


async def connect(plugin: LangGraphPlugin | None = None) -> Client:
    settings = get_settings()
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
        plugins=[plugin or build_langgraph_plugin()],
    )


def build_workflow_runner() -> SandboxedWorkflowRunner:
    """Sandboxed runner with the Pydantic stack passed through.

    The Pydantic data converter imports pydantic_core lazily, after the sandbox
    has taken its snapshot of loaded modules, which makes the Worker emit
    "Module pydantic_core was imported after initial workflow load" warnings.
    Passing these modules through silences that and avoids re-importing a
    compiled extension per workflow task. They are all deterministic
    serialization code, so there is nothing here the sandbox needs to guard.
    """
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(
            "pydantic",
            "pydantic_core",
            "annotated_types",
        )
    )
