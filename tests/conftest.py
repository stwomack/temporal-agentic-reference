"""Test fixtures.

Live Bedrock is a hard requirement of this demo, so nothing here fakes a model
response. Tests that need Bedrock are skipped when the account cannot invoke
the configured model, per the project constraint that a test without live
credentials is skipped rather than mocked.
"""

from __future__ import annotations

import functools

import pytest


@functools.lru_cache(maxsize=1)
def bedrock_skip_reason() -> str | None:
    """Return None if the configured Bedrock model is invokable, else why not."""
    try:
        from common.bedrock import build_chat_model

        build_chat_model(max_tokens=8).invoke("ok")
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


@pytest.fixture(scope="session")
def live_bedrock() -> None:
    reason = bedrock_skip_reason()
    if reason is not None:
        pytest.skip(f"Bedrock not invokable with current credentials. {reason}")


@pytest.fixture(scope="session")
def temporal_skip_reason() -> str | None:
    return None


@pytest.fixture
async def temporal_client():
    """A client against the Temporal server named by the environment.

    Skips rather than failing when no server is reachable, so the unit tests
    still run on a machine with nothing started.
    """
    from common.config import get_settings
    from common.temporal_client import connect

    try:
        return await connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"No Temporal server at {get_settings().temporal_address}. "
            f"Start one with 'temporal server start-dev'. {exc}"
        )
