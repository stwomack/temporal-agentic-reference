"""Amazon Bedrock model client.

Single place where the chat model is constructed, shared by the extraction
agent and by scripts/check_bedrock.py so the preflight check exercises exactly
the same code path as the demo.

Two deliberate choices:

1. `ChatBedrockConverse` (langchain-aws) is the only model client. There is no
   direct Anthropic or OpenAI path anywhere in this repo.
2. botocore retries are disabled and timeouts are short. Retries are Temporal's
   job, and a short timeout turns a credential or connectivity problem into a
   fast, readable error instead of a silent hang.
"""

from __future__ import annotations

from botocore.config import Config
from langchain_aws import ChatBedrockConverse

from common.config import get_settings

# Bedrock calls are wrapped in a Temporal Activity with its own
# start_to_close_timeout and RetryPolicy, so the SDK-level client must not
# retry or block indefinitely.
_BOTO_CONFIG = Config(
    retries={"max_attempts": 1, "mode": "standard"},
    connect_timeout=10,
    read_timeout=60,
)


def build_chat_model(**overrides) -> ChatBedrockConverse:
    """Return a ChatBedrockConverse client configured from the environment."""
    settings = get_settings()
    kwargs = {
        "model_id": settings.bedrock_model_id,
        "region_name": settings.bedrock_region,
        "max_tokens": settings.bedrock_max_tokens,
        "temperature": settings.bedrock_temperature,
        "config": _BOTO_CONFIG,
    }
    kwargs.update(overrides)
    return ChatBedrockConverse(**kwargs)
