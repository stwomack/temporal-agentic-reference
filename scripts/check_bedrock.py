"""Preflight check for story 1.2: is the target Bedrock model invokable?

Run this before anything else:

    uv run python scripts/check_bedrock.py

It performs a real, live invocation through the same ChatBedrockConverse client
the extraction agent uses. Exit code 0 means the demo can run. Any other exit
code prints a specific, actionable reason. Nothing here is mocked, and there is
no fallback that hides a failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow "uv run python scripts/check_bedrock.py" from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.preflight import require_dependencies  # noqa: E402

require_dependencies()

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
)

from common.bedrock import build_chat_model
from common.config import get_settings

PROMPT = "Reply with the single word: ok"


def _fail(message: str, fix: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    print(f"Fix:  {fix}", file=sys.stderr)
    return 1


def main() -> int:
    settings = get_settings()
    print(f"Region:   {settings.bedrock_region}")
    print(f"Model id: {settings.bedrock_model_id}")
    print("Invoking Bedrock (live call, no mock)...")

    try:
        model = build_chat_model(max_tokens=16)
        response = model.invoke(PROMPT)
    except (NoCredentialsError, ProfileNotFound) as exc:
        return _fail(
            f"No usable AWS credentials: {exc}",
            "Run 'aws configure' or export AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN.",
        )
    except EndpointConnectionError as exc:
        return _fail(
            f"Cannot reach the Bedrock endpoint: {exc}",
            f"Check network access and that BEDROCK_REGION "
            f"({settings.bedrock_region}) offers Bedrock.",
        )
    except ReadTimeoutError as exc:
        return _fail(
            f"Bedrock call timed out: {exc}",
            "Retry. If it persists, the region may be saturated. Try another "
            "region or model id.",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        detail = exc.response.get("Error", {}).get("Message", str(exc))
        fixes = {
            "AccessDeniedException": (
                "Grant bedrock:InvokeModel on this model to the calling "
                "principal, and enable model access in the Bedrock console "
                "under Model access."
            ),
            "ValidationException": (
                "Most Anthropic models on Bedrock are inference profile only. "
                "Set BEDROCK_MODEL_ID to a cross region profile id such as "
                "us.anthropic.claude-haiku-4-5-20251001-v1:0."
            ),
            "ResourceNotFoundException": (
                "Either the model id does not exist in this region, or the "
                "provider agreement is missing. For Anthropic models, "
                "'Model use case details have not been submitted' means the "
                "Bedrock console form under Model access has not been filled "
                "out for this account. Check with "
                "'aws bedrock get-use-case-for-model-access' and "
                "'aws bedrock get-foundation-model-availability --model-id "
                "<id>'. List valid ids with "
                "'aws bedrock list-foundation-models --by-provider anthropic'."
            ),
            "ThrottlingException": (
                "Request was throttled. Retry, or request a quota increase for "
                "this model."
            ),
            "UnrecognizedClientException": (
                "Credentials were rejected. Verify the access key and that it "
                "belongs to the intended account."
            ),
        }
        return _fail(
            f"Bedrock rejected the call ({code}): {detail}",
            fixes.get(code, "Inspect the error code above against the Bedrock API docs."),
        )
    except Exception as exc:  # noqa: BLE001 - preflight must never hang or hide a cause
        return _fail(
            f"Unexpected error of type {type(exc).__name__}: {exc}",
            "This is not a known Bedrock failure mode. Read the traceback above.",
        )

    text = (response.content if isinstance(response.content, str) else str(response.content)).strip()
    usage = getattr(response, "usage_metadata", None) or {}
    print(f"OK: model responded with {text!r}")
    print(
        f"Tokens: input={usage.get('input_tokens', 0)} "
        f"output={usage.get('output_tokens', 0)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
