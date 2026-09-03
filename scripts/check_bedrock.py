"""Preflight check for story 1.2: is the target Bedrock model invokable?

Run this before anything else:

    uv run python scripts/check_bedrock.py

It performs a real, live invocation through the same ChatBedrockConverse client
the extraction agent uses. Exit code 0 means the demo can run. Any other exit
code prints a specific, actionable reason. Nothing here is mocked, and there is
no fallback that hides a failure.
"""

from __future__ import annotations

import os
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

BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"


def _bearer_token_set() -> bool:
    """Whether a Bedrock API key is present in the environment.

    botocore derives this variable name from the service signing name, so when
    it is set it authenticates Bedrock calls in place of SigV4 credentials, and
    it does so for bedrock endpoints only. A stale key therefore fails here
    while `aws sts get-caller-identity` and every other AWS call keep working,
    which is an unreadable pair of symptoms unless the check names the auth
    path it is actually using. The value is never printed.
    """
    return bool(os.environ.get(BEARER_ENV))


# A real Bedrock API key is a long encoded blob. Short term keys observed in
# practice run past 2000 characters and long term keys are still well over a
# hundred, so anything this short is a copy that lost most of its payload. The
# console modal elides the value on screen, and copying the visible text or its
# tooltip instead of using the copy button produces exactly that.
MIN_PLAUSIBLE_KEY_LENGTH = 100


def _bearer_token_problems() -> list[str]:
    """Structural problems with the configured key, in plain words.

    Everything here is derived from length and character class. The value is
    never printed, logged, or returned.
    """
    key = os.environ.get(BEARER_ENV, "")
    problems = []
    if len(key) < MIN_PLAUSIBLE_KEY_LENGTH:
        problems.append(
            f"it is only {len(key)} characters, far short of a real key, so the "
            "copy lost its payload"
        )
    if any(c.isspace() for c in key):
        problems.append(
            "it contains whitespace, so the shell split the value on export. "
            "Wrap it in single quotes"
        )
    if not key.startswith(("bedrock-api-key-", "ABSK")):
        problems.append("it does not start with a recognized Bedrock key prefix")
    return problems


def _fail(message: str, fix: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    print(f"Fix:  {fix}", file=sys.stderr)
    return 1


def main() -> int:
    settings = get_settings()
    print(f"Region:   {settings.bedrock_region}")
    print(f"Model id: {settings.bedrock_model_id}")
    print(
        "Auth:     "
        + (
            f"Bedrock API key from {BEARER_ENV}"
            if _bearer_token_set()
            else "SigV4 credentials from the standard chain"
        )
    )
    print("Invoking Bedrock (live call, no mock)...")

    try:
        model = build_chat_model(max_tokens=16)
        response = model.invoke(PROMPT)
    except (NoCredentialsError, ProfileNotFound) as exc:
        return _fail(
            f"No usable AWS credentials: {exc}",
            "Run 'aws configure' or export AWS_ACCESS_KEY_ID / "
            "AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN. A Bedrock API "
            f"key in {BEARER_ENV} also works on its own.",
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
        # A rejected API key reports as AccessDeniedException, the same code an
        # IAM problem uses, so the generic fix below would send the reader off
        # to check policies that are not the cause.
        if _bearer_token_set() and (
            code in ("AccessDeniedException", "UnrecognizedClientException")
            or "API Key" in detail
        ):
            problems = _bearer_token_problems()
            if problems:
                fix = (
                    f"The key is malformed before Bedrock ever sees it: "
                    f"{'; '.join(problems)}. The console elides the value on "
                    "screen, so copy it with the modal's copy button rather "
                    "than selecting the visible text."
                )
            else:
                # The key survived the shell intact, so the rejection is about
                # the key itself. Region is the first thing to check: a key is
                # minted for the region the console was showing, and calling a
                # different one fails with this same generic message.
                fix = (
                    "The key is well formed, so this is not a copy problem. "
                    "Check that it was generated with the Bedrock console set "
                    f"to {settings.bedrock_region}, the region this demo calls. "
                    "A key minted in another region fails here with exactly "
                    "this error. Then check its type: short term keys expire "
                    "within 12 hours, and long term keys are the right choice "
                    "for anything you hand to someone else."
                )
            return _fail(
                f"Bedrock rejected the API key ({code}): {detail}",
                f"{fix} Or run 'unset {BEARER_ENV}' to fall back to SigV4 "
                "credentials. It overrides SigV4 for Bedrock only, so other AWS "
                "commands keep working while this one fails.",
            )
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
