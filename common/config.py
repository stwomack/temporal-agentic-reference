"""Central configuration for the purchase order approval demo.

Every tunable value is read from the environment so the same code can run a
local demo, a CI check, or a shared deployment without edits. Defaults are
chosen so that `uv run python worker.py` works against a local Temporal dev
server with no .env file present.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Temporal. The variable names match the ones the Temporal CLI uses, so a
    # .env that already works with the CLI works here unchanged.
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "po-approval"

    # Temporal Cloud connectivity. Leave all of these unset for a local dev
    # server. Set either an API key or an mTLS client certificate pair, not
    # both. TLS is turned on automatically when any of them is set, or when the
    # address is a *.tmprl.cloud host; TEMPORAL_TLS forces it either way.
    temporal_tls: Optional[bool] = None
    temporal_api_key: Optional[str] = None
    temporal_tls_client_cert_path: Optional[str] = None
    temporal_tls_client_key_path: Optional[str] = None
    temporal_tls_server_ca_path: Optional[str] = None
    temporal_tls_server_name: Optional[str] = None

    # Where to send "view in Temporal" links. Defaults to the local dev
    # server's web UI, or to Temporal Cloud when the address is a Cloud host.
    temporal_ui_url: Optional[str] = None

    @property
    def temporal_host(self) -> str:
        return self.temporal_address.rsplit(":", 1)[0]

    @property
    def uses_mtls(self) -> bool:
        return bool(
            self.temporal_tls_client_cert_path or self.temporal_tls_client_key_path
        )

    @property
    def looks_like_temporal_cloud(self) -> bool:
        return self.temporal_host.endswith(".tmprl.cloud")

    @property
    def tls_enabled(self) -> bool:
        """Whether to negotiate TLS.

        Explicit TEMPORAL_TLS wins. Otherwise any credential, or a Temporal
        Cloud address, implies TLS. Connecting in plaintext to a TLS only
        endpoint fails with an opaque "transport error ... broken pipe", so
        guessing right here saves a confusing debugging session.
        """
        if self.temporal_tls is not None:
            return self.temporal_tls
        return bool(
            self.temporal_api_key or self.uses_mtls or self.looks_like_temporal_cloud
        )

    # Amazon Bedrock. Model access is verified by scripts/check_bedrock.py.
    #
    # The intended target for this demo is Claude Haiku 4.5
    # ("us.anthropic.claude-haiku-4-5-20251001-v1:0"). On the account this repo
    # was built against, every Anthropic model is gated behind the Bedrock
    # "Anthropic use case details" form, which has not been submitted, so the
    # default is a model that is actually invokable today. Switching is one
    # env var once that form is approved. See README, section "Bedrock model
    # access".
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"
    bedrock_max_tokens: int = 2048
    bedrock_temperature: float = 0.0

    # start_to_close_timeout for an agent's model turn. This is also how long
    # Temporal takes to notice a worker died mid call: a non heartbeating
    # activity cannot be rescheduled until its timeout expires, so a crash
    # landing inside a model turn stalls the workflow for this long. Observed
    # turns run 1.5 to 6 seconds, so the default is generous. Lower it to
    # around 20 when you intend to demo a worker crash by hand.
    agent_activity_timeout_seconds: int = 60

    # Per agent model overrides. Each falls back to bedrock_model_id when
    # unset, so the demo runs on one model by default but can put a cheaper
    # model on the mechanical agents and a stronger one on the supervisor.
    bedrock_model_extraction: Optional[str] = None
    bedrock_model_vendor_risk: Optional[str] = None
    bedrock_model_policy: Optional[str] = None
    bedrock_model_duplicate: Optional[str] = None
    bedrock_model_supervisor: Optional[str] = None

    def model_for(self, agent: str) -> str:
        """Model id for one agent, falling back to the shared default."""
        override = {
            "extraction": self.bedrock_model_extraction,
            "vendor_risk": self.bedrock_model_vendor_risk,
            "policy_compliance": self.bedrock_model_policy,
            "duplicate_detection": self.bedrock_model_duplicate,
            "supervisor": self.bedrock_model_supervisor,
        }.get(agent)
        return override or self.bedrock_model_id

    # Guardrail policy. Amounts are in the PO currency, assumed USD for the demo.
    po_approval_threshold: float = Field(
        default=10_000.0,
        description="Above this amount a PO needs a human approval decision.",
    )
    po_hard_block_threshold: float = Field(
        default=250_000.0,
        description="Above this amount a PO is rejected outright, no human path.",
    )
    po_blocked_vendors: str = Field(
        default="Acme Shell Corp,Unverified Holdings LLC",
        description="Comma separated vendor names that are always rejected.",
    )

    # Simulated ERP submission
    erp_seeded_failures: int = Field(
        default=0,
        description="Number of leading ERP attempts that fail before one succeeds.",
    )
    erp_max_attempts: int = Field(
        default=5, description="Temporal RetryPolicy maximum_attempts for ERP submit."
    )

    # Human in the loop
    approval_timeout_seconds: int = 900

    # API / UI
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @property
    def temporal_ui_base_url(self) -> str:
        if self.temporal_ui_url:
            return self.temporal_ui_url.rstrip("/")
        if self.looks_like_temporal_cloud:
            return "https://cloud.temporal.io"
        return f"http://{self.temporal_host}:8233"

    @property
    def blocked_vendor_list(self) -> list[str]:
        return [v.strip().lower() for v in self.po_blocked_vendors.split(",") if v.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
