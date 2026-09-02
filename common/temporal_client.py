"""Temporal client and plugin wiring, shared by the worker and the API.

Both processes must build the same `LangGraphPlugin` instance shape and use the
same data converter, otherwise the workflow input and the graph state will not
round trip.

Connects to either a local dev server or Temporal Cloud. Which one is decided
entirely by environment variables, using the same names the Temporal CLI uses,
so a .env that already works with the CLI works here unchanged. See
common/config.py and .env.example.
"""

from __future__ import annotations

from pathlib import Path

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import TLSConfig
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from agents import (
    duplicate_detection,
    policy_compliance,
    supervisor,
    vendor_risk,
)
from agents.extraction_graph import build_extraction_graph
from common.config import Settings, get_settings
from common.constants import (
    DUPLICATE_GRAPH_NAME,
    EXTRACTION_GRAPH_NAME,
    POLICY_GRAPH_NAME,
    SUPERVISOR_GRAPH_NAME,
    VENDOR_RISK_GRAPH_NAME,
)


class TemporalConnectionError(RuntimeError):
    """A connection problem stated in terms the operator can act on."""


def build_langgraph_plugin() -> LangGraphPlugin:
    """The LangGraph plugin, in Public Preview in the Temporal Python SDK.

    Registers all five agents. Every node marked execute_in="activity" becomes
    a Temporal Activity, so each model turn and each tool call is separately
    retried, timed, and recorded in workflow history.

    Each agent is its own graph rather than one big graph, because the
    orchestrator workflow is what fans them out. That keeps Temporal as the
    multi-agent orchestrator: the concurrency, the retries, and the durability
    of partial results are all Temporal's, not LangGraph's.
    """
    return LangGraphPlugin(
        graphs={
            EXTRACTION_GRAPH_NAME: build_extraction_graph(),
            VENDOR_RISK_GRAPH_NAME: vendor_risk.build_graph(),
            POLICY_GRAPH_NAME: policy_compliance.build_graph(),
            DUPLICATE_GRAPH_NAME: duplicate_detection.build_graph(),
            SUPERVISOR_GRAPH_NAME: supervisor.build_graph(),
        }
    )


def _read_credential(path_value: str, label: str) -> bytes:
    """Read a PEM file, failing with the path rather than an OSError."""
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise TemporalConnectionError(
            f"{label} does not exist: {path}\n"
            f"Set it to the PEM file you downloaded from Temporal Cloud, or "
            f"unset it to connect without mTLS."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TemporalConnectionError(f"Cannot read {label} at {path}: {exc}") from exc


def build_tls_config(settings: Settings) -> TLSConfig | bool:
    """Return the tls= argument for Client.connect.

    True means "negotiate TLS with the system trust store", which is what
    Temporal Cloud API key auth wants. A TLSConfig carries an mTLS client
    certificate pair, a private CA, or an overridden SNI name.
    """
    if not settings.tls_enabled:
        return False

    cert_path = settings.temporal_tls_client_cert_path
    key_path = settings.temporal_tls_client_key_path
    if bool(cert_path) != bool(key_path):
        missing = (
            "TEMPORAL_TLS_CLIENT_KEY_PATH" if cert_path else "TEMPORAL_TLS_CLIENT_CERT_PATH"
        )
        raise TemporalConnectionError(
            f"mTLS needs both a certificate and a private key, but {missing} is "
            f"not set. Set both, or neither plus TEMPORAL_API_KEY instead."
        )

    if settings.temporal_api_key and cert_path:
        raise TemporalConnectionError(
            "Both TEMPORAL_API_KEY and an mTLS certificate pair are set. "
            "Temporal Cloud accepts one or the other. Unset whichever you are "
            "not using."
        )

    client_cert = (
        _read_credential(cert_path, "TEMPORAL_TLS_CLIENT_CERT_PATH") if cert_path else None
    )
    client_key = (
        _read_credential(key_path, "TEMPORAL_TLS_CLIENT_KEY_PATH") if key_path else None
    )
    server_ca = (
        _read_credential(settings.temporal_tls_server_ca_path, "TEMPORAL_TLS_SERVER_CA_PATH")
        if settings.temporal_tls_server_ca_path
        else None
    )

    # Plain TLS with the system trust store: no TLSConfig needed.
    if not any([client_cert, client_key, server_ca, settings.temporal_tls_server_name]):
        return True

    return TLSConfig(
        client_cert=client_cert,
        client_private_key=client_key,
        server_root_ca_cert=server_ca,
        domain=settings.temporal_tls_server_name,
    )


def describe_target(settings: Settings) -> str:
    """One line summary of what we are connecting to, safe to log.

    Never includes the API key or any key material, only whether they are set.
    """
    if settings.temporal_api_key:
        auth = "api key"
    elif settings.uses_mtls:
        auth = "mTLS client certificate"
    else:
        auth = "none"
    return (
        f"address={settings.temporal_address} "
        f"namespace={settings.temporal_namespace} "
        f"tls={'on' if settings.tls_enabled else 'off'} auth={auth}"
    )


def _connection_advice(settings: Settings, exc: Exception) -> str:
    """Turn the SDK's transport level error into something actionable."""
    message = str(exc)
    lines = [f"Cannot connect to Temporal. {describe_target(settings)}"]

    if "broken pipe" in message.lower() or "transport error" in message.lower():
        if not settings.tls_enabled:
            lines.append(
                "A 'broken pipe' or 'transport error' during get_system_info "
                "almost always means the server requires TLS and the client "
                "connected in plaintext. Set TEMPORAL_TLS=true, or set "
                "TEMPORAL_API_KEY or the TEMPORAL_TLS_CLIENT_CERT_PATH and "
                "TEMPORAL_TLS_CLIENT_KEY_PATH pair."
            )
        else:
            lines.append(
                "TLS is on, so check that the address is the gRPC endpoint on "
                "port 7233 (not the web UI host), and that the certificate pair "
                "matches the namespace."
            )
    elif "connection refused" in message.lower():
        lines.append(
            "Nothing is listening on that address. For a local demo run "
            "'temporal server start-dev' in another terminal."
        )
    elif "unauthenticated" in message.lower() or "permission" in message.lower():
        lines.append(
            "The server rejected the credentials. Check that the API key or "
            "certificate has not expired and is scoped to this namespace."
        )

    if settings.looks_like_temporal_cloud:
        lines.append(
            "For Temporal Cloud the namespace must be the fully qualified "
            "'namespace.account_id' form."
        )

    lines.append(f"Underlying error: {message}")
    return "\n".join(lines)


async def connect(plugin: LangGraphPlugin | None = None) -> Client:
    settings = get_settings()
    tls = build_tls_config(settings)
    try:
        return await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            api_key=settings.temporal_api_key,
            tls=tls,
            data_converter=pydantic_data_converter,
            plugins=[plugin or build_langgraph_plugin()],
        )
    except TemporalConnectionError:
        raise
    except Exception as exc:
        raise TemporalConnectionError(_connection_advice(settings, exc)) from exc


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
