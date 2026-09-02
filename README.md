# Durable multi-agent purchase order approval

A demo of a purchase order approval process that survives failures, pauses for
a human, and enforces spend policy. Temporal orchestrates the process. A
LangGraph agent backed by Amazon Bedrock reads the purchase order. Everything
is triggerable from a browser page, with no terminal needed once the two
processes are running.

The pipeline has four steps:

1. **Extraction agent.** A LangGraph graph whose model node runs as a Temporal
   Activity through the Temporal LangGraph plugin. It calls Amazon Bedrock via
   `ChatBedrockConverse` and returns structured fields: vendor, requester,
   currency, total, and line items. This call is live on every single run.
2. **Policy guardrail.** A deterministic, rule based Activity. Under the
   threshold it passes, over the threshold it asks for a human, and for a
   blocked vendor or an amount over the hard cap it rejects outright.
3. **Human approval.** Only when the guardrail asks for it. The workflow parks
   on `workflow.wait_condition` and is woken by a Temporal Update. There is no
   polling anywhere in the approval path.
4. **ERP submission.** A simulated submission with a configurable seeded
   failure count. Retries are handled entirely by Temporal's RetryPolicy. The
   Activity contains no retry code of its own.

## What you need

- Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/)
- The [Temporal CLI](https://docs.temporal.io/cli#install)
- AWS credentials in the standard chain (environment variables, a profile, SSO,
  or an instance role) with `bedrock:InvokeModel` on the model you configure

No Temporal background is assumed. The steps below are the whole story.

## Setup

```bash
uv sync --extra dev
cp .env.example .env      # optional, the defaults work as is
```

Run every command in this README through `uv run`. That is what puts the
project's virtual environment on the path. A bare `python scripts/...` uses
your system interpreter, which does not have the dependencies; the scripts
detect that and tell you, but it is easier to just prefix with `uv run`.

Confirm Bedrock is reachable before anything else. This performs a real model
invocation and prints a specific reason if it cannot:

```bash
./scripts/check_bedrock.sh
```

That is a thin wrapper around `uv run python scripts/check_bedrock.py`, which
works just as well.

A successful run looks like this:

```
Region:   us-east-1
Model id: us.amazon.nova-pro-v1:0
Invoking Bedrock (live call, no mock)...
OK: model responded with 'Ok.'
Tokens: input=7 output=3
```

If it fails, fix that first. Nothing downstream can work without it, and
nothing in this repo falls back to a canned model response.

## Bedrock model access

The intended target model for this demo is Claude Haiku 4.5
(`us.anthropic.claude-haiku-4-5-20251001-v1:0`).

On the AWS account this repo was built against, every Anthropic model on
Bedrock returns:

```
ResourceNotFoundException: Model use case details have not been submitted for
this account. Fill out the Anthropic use case details form before using the
model.
```

That gate is account wide and cannot be lifted from code. It requires
submitting the Anthropic use case details form in the Bedrock console, under
Model access, with your organization's details. You can confirm the current
state with:

```bash
aws bedrock get-use-case-for-model-access
aws bedrock get-foundation-model-availability \
    --model-id anthropic.claude-haiku-4-5-20251001-v1:0
```

So the default model is `us.amazon.nova-pro-v1:0`, which is invokable on that
account today and handles the extraction task well. Nothing about the demo is
model specific. Once the form is approved, switch with one variable:

```bash
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

## Running it

Three processes, each in its own terminal.

**1. The Temporal server.** A local development server, in memory:

```bash
temporal server start-dev
```

That gives you the Temporal Web UI at http://localhost:8233. Leave it running.

**2. The worker.** This is the process that actually executes your workflow and
activity code. Temporal never runs your code itself; it hands work to workers.

```bash
uv run python worker.py
```

**3. The API and UI.**

```bash
uv run uvicorn api.main:app --reload
```

Open http://127.0.0.1:8000. The header shows which Temporal server and which
Bedrock model are in use, so you can tell at a glance that you are pointed
where you think you are.

## The five scenarios

Each button on the page starts a fresh workflow. All five run the same workflow
code and all five make a live Bedrock call. Only the policy thresholds and the
seeded ERP failure count differ.

| Scenario | What it shows | Ends as |
|---|---|---|
| Happy path | A routine order under the threshold. Extract, check, submit. | `submitted` |
| Guardrail violation | A blocked vendor. Rejected by policy with no human path, and the ERP step never runs. | `rejected_by_policy` |
| Human in the loop: approve | A capital order over the threshold. The workflow pauses, you approve, it submits. | `submitted` |
| Human in the loop: reject | The same pause, but you reject. A distinct end state, and ERP is never called. | `rejected_by_human` |
| Failure and retry | The ERP gateway fails twice. Temporal retries with backoff and the third attempt succeeds. | `submitted` |

What to watch, in order:

- **The diagram** highlights the running step and fills in the latency of each
  finished step. It updates on its own, over a server sent event stream. You
  never need to refresh.
- **The extracted purchase order** panel shows the fields the model pulled out,
  along with the model id and the token counts for that specific call. Those
  counts are the proof the call was live.
- **The approval panel** appears only in the two human in the loop scenarios.
  Approve or Reject sends a Temporal Update. If you try to send a decision when
  the workflow is not waiting, the API returns the workflow's own reason rather
  than a generic error.
- **The retry banner** appears in the failure and retry scenario, showing which
  attempt Temporal is on and the message from the last failure.
- **The telemetry table** lists every step with status, latency, and timestamp.
  Skipped steps say why they were skipped.
- **The code panel** on the right follows along. See below.

Click "view in Temporal" next to the run id to open the same execution in the
Temporal Web UI. In the failure and retry scenario, while it is running, the
Pending Activities section there shows the live attempt count and the last
failure. After it completes, the `ActivityTaskStarted` event for the ERP step
records `attempt: 3` along with the failure that preceded it.

## The code panel (story 4.1)

Story 4.1 was built. The right hand panel shows the source file and function
that implements the currently executing step, with the function body
highlighted and scrolled into view, and it follows the workflow from step to
step with no manual refresh.

The line numbers are not hardcoded. The API parses the module with Python's
`ast` at request time and finds the function by name, so the panel keeps
pointing at the right code after the files are edited. A test asserts that
every step in the diagram still resolves to a real function, which is what
would otherwise break silently after a rename.

## Driving it from the command line instead

The UI is not required. To run a scenario without a browser:

```bash
uv run python scripts/run_scenario.py happy_path
uv run python scripts/run_scenario.py human_approval --decision approve
uv run python scripts/run_scenario.py human_rejection --decision reject
uv run python scripts/run_scenario.py erp_retry
```

The workflow also accepts a Signal named `decide`, in addition to the
`submit_decision` Update, so you can drive the approval step with the Temporal
CLI:

```bash
temporal workflow signal --workflow-id po-PO-XXXXXXXX --name decide \
  --input '{"approved": true, "decided_by": "cli", "comment": "ok"}'
```

## Configuration

Everything tunable is an environment variable, documented in `.env.example`:
the Temporal address and task queue, the Bedrock region and model, the approval
threshold, the hard block threshold, the blocked vendor list, the seeded ERP
failure count, the ERP retry ceiling, and the approval deadline.

Values that steer workflow control flow, such as the retry ceiling and the
approval deadline, are read once when a workflow starts and carried in its
input rather than read inside the workflow. That keeps history replay
deterministic even if a worker restarts with different variables set.

## Tests

```bash
uv run pytest
```

The suite covers the guardrail policy boundaries, the ERP activity's behavior
at each Temporal attempt number, the code panel's source resolution, the
deterministic normalization step, live Bedrock extraction, and all five
scenarios end to end against a real Temporal server.

Tests that need Bedrock or a Temporal server are skipped, with the reason
printed, when those are not available. They are never backed by a fake model
response. The whole point of the demo is that the model call is real, so a
passing test suite that stubbed it out would be worse than a skipped one.

## Notes on the design

- The workflow's `status` query is the single source of truth for the UI. The
  API holds no state, the browser holds no model of the pipeline, and so the
  diagram, the telemetry feed, and the code panel cannot drift from what the
  workflow actually did.
- The extraction agent is the only probabilistic component. Policy enforcement
  is deliberately rule based so it is auditable and gives the same answer every
  time.
- The extraction graph has two nodes: the Bedrock call runs as an Activity
  because it does I/O and needs retries and timeouts, and the normalization
  step runs on the workflow thread because it is pure and cheap. Both
  execution locations are exercised on every run.
- The Temporal LangGraph plugin is in Public Preview.

## A gotcha worth knowing

Since Temporal Python SDK 1.32, a Worker inherits the plugins registered on its
Client. Passing the same `LangGraphPlugin` to both, which older examples show,
registers the graph's node activities twice and the Worker refuses to start
with `More than one activity named po-extraction.extract_po`. Register it on
the Client only.
