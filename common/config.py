"""Central configuration for the purchase order approval demo.

Every tunable value is read from the environment so the same code can run a
local demo, a CI check, or a shared deployment without edits. Defaults are
chosen so that `uv run python worker.py` works against a local Temporal dev
server with no .env file present.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Temporal
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "po-approval"

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
    def blocked_vendor_list(self) -> list[str]:
        return [v.strip().lower() for v in self.po_blocked_vendors.split(",") if v.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
