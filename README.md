# Durable multi-agent purchase order approval

Five Bedrock-backed agents reviewing a purchase order, orchestrated by Temporal,
with a deterministic guardrail and a human in the loop. Everything is
triggerable from a browser page.

Temporal is the multi-agent orchestrator here. The fan out, the retries, the
per-agent timeouts, and the durability of partial results all belong to
Temporal rather than to an agent framework.

```
                    Extraction agent
                           |
        +------------------+------------------+     concurrent, one workflow task
        |                  |                  |
   Vendor risk       Policy compliance   Duplicate detection
   (tool loop)                            (tool loop)
        |                  |                  |
        +------------------+------------------+
                           |
                   Supervisor agent
              auto approve / escalate / reject
                           |
          Deterministic guardrail  ->  Human  ->  ERP
```

**The five agents.** Each is its own LangGraph graph with its own model,
timeout, and retry policy, and each runs as its own Temporal activity.

1. **Extraction agent** reads the free-form request into structured fields:
   vendor, requester, currency, total, line items.
2. **Vendor risk agent** runs a tool loop over the vendor registry. It looks the
   vendor up, pulls the incident history when the registry gives it a reason
   to, and rates the risk from low to critical.
3. **Policy compliance agent** reads the written procurement policy in
   `data/procurement_policy.md` and cites the clauses a request violates. This
   is the prose half of policy, the half a numeric rule cannot express.
4. **Duplicate detection agent** runs a tool loop over the purchase order
   history, looking for a re-submitted order or a purchase split across several
   orders to duck an approval threshold.
5. **Supervisor agent** reads all three findings and routes the request: auto
   approve, escalate to a human, or reject.

**The guardrail outranks the agents.** After the supervisor decides, a
deterministic, rule-based check runs on spending thresholds and blocked
vendors. It has the final say, so an agent cannot talk a hard block into an
approval. The agents add judgment on top of rules they cannot weaken.

**Then the usual pipeline.** Human approval when the supervisor escalates or
the threshold demands it, delivered as a Temporal Update with no polling, and a
simulated ERP submission whose retries are Temporal's.

## What this demonstrates that a script cannot

- **The fan out is real.** The three specialists dispatch from a single
  workflow task. Workflow history shows three activities in flight at once.
- **Partial work survives a crash.** Kill the worker mid fan out and the
  specialists that already finished are not re-run. Their Bedrock calls are not
  paid for twice. `./scripts/crash_demo.sh` proves this by running the same
  request twice, once normally and once with a `SIGKILL`, and comparing the
  activity execution counts.
- **Every agent turn is independently retried.** A model call that throttles
  retries on its own policy, with backoff, without disturbing the other
  agents. Three specialists firing at once makes Bedrock throttling routine
  rather than exceptional, and it is absorbed instead of failing the run.
- **Agents and rules are layered, not mixed.** The escalation scenario is under
  the spending threshold, so the rule passes it, and the agents stop it anyway.

## What you need

- Python 3.12 or 3.13, and [uv](https://docs.astral.sh/uv/)
- The [Temporal CLI](https://docs.temporal.io/cli#install)
- AWS credentials in the standard chain (environment variables, a profile, SSO,
  or an instance role) with `bedrock:InvokeModel` on the model you configure

The demo never shells out to the AWS CLI. It reads credentials through boto3,
so the CLI is optional and a Bedrock API key on its own is enough. See
"Running with a Bedrock API key" below.

No Temporal background is assumed. The steps below are the whole story.

## Setup

```bash
uv sync --extra dev
cp .env.example .env      # optional, the defaults work as is
```

`uv.lock` is committed, so `uv sync` installs the exact versions this was built
and tested against. That matters here more than usual: the Temporal LangGraph
plugin is in Public Preview and its API can move between releases.

Everything below has a wrapper in `scripts/` that handles the `uv run`
invocation for you, works from any directory, and passes extra arguments
through. Use those, or use the underlying `uv run ...` command; both are shown.

What matters is that the project's virtual environment is on the path. A bare
`python scripts/...` uses your system interpreter, which does not have the
dependencies. The scripts detect that and tell you what to run instead.

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

## Running with a Bedrock API key

The lowest friction way to run this on a machine with no AWS setup. One
environment variable, no profile, no CLI:

```bash
export AWS_BEARER_TOKEN_BEDROCK='...'    # single quotes, see below
./scripts/check_bedrock.sh
```

Generate the key in the Bedrock console under API keys. Four things decide
whether it works, and all four produce the same unhelpful `Authentication
failed: Please make sure your API Key is valid`, so check them in this order.

**Generate it in the region the demo calls.** This is the most common cause,
and it has bitten this repo. A key is minted for whatever region the console
was showing, so a key made in us-west-2 fails every call to us-east-1 with the
message above and no hint that the region is why. If you are not sure which
region a key belongs to, sweep for it with the key still exported. The check
prints the auth path it used on every run, so if those lines say SigV4 rather
than Bedrock API key, the key is not in the environment and the sweep is
testing your credentials instead:

```bash
for r in us-east-1 us-east-2 us-west-2; do
  echo "== $r"; BEDROCK_REGION=$r ./scripts/check_bedrock.sh
done
```

The region that stops saying `Authentication failed` is the key's home. Either
regenerate the key in your `BEDROCK_REGION`, or set `BEDROCK_REGION` to the
region the key came from.

**Prefer a long term key.** Short term keys expire within 12 hours, which is
fine for a demo you are giving this afternoon and wrong for anything you hand
to someone else.

**Quote it on export.** These keys are long and can contain characters the
shell will act on. Unquoted, the value is silently truncated at the first
space.

**Enable model access for the model you configure.** A valid key on an account
without model access fails just the same. See the next section.

Each person runs their own key from their own account. One key shared across a
team bills and rate limits against a single identity, makes CloudWatch unable
to tell anyone apart, and means revoking one person revokes everybody.

One property worth knowing when this goes wrong: the variable overrides normal
credentials for Bedrock calls and nothing else. A bad key fails this demo while
every other AWS command on the machine keeps working, which reads as a broken
demo rather than a bad credential. `./scripts/check_bedrock.sh` prints which
auth path it used and separates a mangled key from a rejected one.

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
Model access, with your organization's details.

On a fresh account this applies to whichever model you configure, Nova
included: model access is granted per model, per region, and is separate from
IAM permissions. Enable it in the console before the first run.

You can confirm the current state with the AWS CLI, which the API key path
above does not otherwise need:

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

## Pointing at Temporal Cloud instead

The demo runs against either a local dev server or Temporal Cloud, decided
entirely by environment variables. The variable names are the ones the Temporal
CLI uses, so a `.env` that already works with the CLI works here unchanged.

For Cloud, set the gRPC endpoint and the fully qualified namespace, then pick
one auth method:

```bash
TEMPORAL_ADDRESS=your-namespace.acct.tmprl.cloud:7233
TEMPORAL_NAMESPACE=your-namespace.acct

# either an API key
TEMPORAL_API_KEY=...

# or an mTLS client certificate pair
TEMPORAL_TLS_CLIENT_CERT_PATH=/path/to/client.pem
TEMPORAL_TLS_CLIENT_KEY_PATH=/path/to/client.key
```

TLS is turned on automatically for any `*.tmprl.cloud` address, and whenever
either credential is set. `TEMPORAL_TLS=true` or `false` forces it. The
"view in Temporal" links switch to https://cloud.temporal.io on their own.

Skip step 1 below when using Cloud; the worker and the API are the same.

If the address points at a TLS endpoint but TLS is off, the SDK fails with
`transport error ... broken pipe` from `get_system_info`, which says nothing
useful. The connection code detects that case and prints what to set instead.

## Running it

Three processes, each in its own terminal.

**1. The Temporal server.** Skip this if you are using Temporal Cloud. For a
local demo, a development server, in memory:

```bash
temporal server start-dev
```

That gives you the Temporal Web UI at http://localhost:8233. Leave it running.

**2. The worker.** This is the process that actually executes your workflow and
activity code. Temporal never runs your code itself; it hands work to workers.

```bash
./scripts/worker.sh          # or: uv run python worker.py
```

It logs the address, namespace, whether TLS is on, which auth method is in use,
the task queue, and the Bedrock model, so you can confirm where it connected.

**3. The API and UI.**

```bash
./scripts/api.sh             # or: uv run uvicorn api.main:app --reload
```

Open http://127.0.0.1:8000. The header shows which Temporal namespace and which
Bedrock model are in use, so you can tell at a glance that you are pointed
where you think you are.

The wrapper reads `API_HOST` and `API_PORT` through the project's settings, so
setting them in `.env` moves the server. Extra flags pass through to uvicorn,
and because uvicorn lets the later flag win, `./scripts/api.sh --port 9000`
overrides the configured port for one run.

## The seven scenarios

Each button on the page starts a fresh workflow. All seven run the same workflow
code and all seven make live Bedrock calls through all five agents. Only the
request text, the policy thresholds, and the seeded ERP failure count differ.

| Scenario | What it shows | Ends as |
|---|---|---|
| Happy path | Every agent clean, supervisor auto approves, straight to ERP. | `submitted` |
| Guardrail violation | An unapproved vendor. The vendor risk agent rates it critical and the supervisor rejects, then the deterministic guardrail blocks it independently. Two layers, same answer. | `rejected_by_policy` |
| Human in the loop: approve | Over the threshold. The workflow pauses, you approve, it submits. | `submitted` |
| Human in the loop: reject | The same pause, but you reject. ERP is never called. | `rejected_by_human` |
| Agent judgment escalation | Under the threshold, so the rule passes it. The agents find a probationary vendor with open cold chain findings and escalate anyway. | `submitted` after your approval |
| Duplicate catch | A re-submission of an order placed nine days ago, reworded, with a slightly different total. No rule catches this. The duplicate detection agent reads the history and does. | `submitted` after your approval |
| Failure and retry | The ERP gateway fails twice. Temporal retries with backoff and the third attempt succeeds. | `submitted` |

The two worth spending time on with an audience are **Agent judgment
escalation**, because the deterministic guardrail visibly says "no approval
needed" while the agents stop the request anyway, and **Duplicate catch**,
because it is the case where a rule cannot substitute for reading.

What to watch, in order:

- **The diagram** highlights the running step and fills in the latency of each
  finished step. The three specialists sit inside a dashed box marked
  "concurrent" and light up together. It updates on its own, over a server sent
  event stream. You never need to refresh.
- **The agent findings panel** fills in as each agent reports, with its
  severity, its reasoning, and the model, turn count, tools called, and tokens
  for that specific agent.
- **The extracted purchase order** panel shows the fields the model pulled out,
  along with the model id and the token counts for that specific call. Those
  counts are the proof the call was live.
- **The approval panel** appears whenever a human is needed, which is the two
  human in the loop scenarios plus agent judgment escalation and duplicate
  catch. It states why, which may be the supervisor's reasoning rather than a
  threshold. Approve or Reject sends a Temporal Update. If you try to send a
  decision when the workflow is not waiting, the API returns the workflow's own
  reason rather than a generic error.
- **The retry banner** appears in the failure and retry scenario, showing which
  attempt Temporal is on and the message from the last failure.
- **The telemetry table** lists every step with status, latency, and timestamp.
  Skipped steps say why they were skipped.

Click "view in Temporal" next to the run id to open the same execution in the
Temporal Web UI. In the failure and retry scenario, while it is running, the
Pending Activities section there shows the live attempt count and the last
failure. After it completes, the `ActivityTaskStarted` event for the ERP step
records `attempt: 3` along with the failure that preceded it.

## The durability demo

This is the part worth showing to anyone who thinks a Python script would do.

Run it with no worker of your own running:

```bash
./scripts/cleanup.sh --all     # stop any worker first
./scripts/crash_demo.sh
```

The script starts and kills its own worker. A second worker on the same task
queue would pick the work up the instant the first one dies, so the fan out
would never actually be interrupted and the comparison at the end would print
"same" for everything while proving nothing. The script checks for other
pollers and refuses to run rather than hand you that misleading result. Pass
`--force` to override.

It judges a poller live by its last access time, not by its presence, because
Temporal keeps an entry for minutes after the worker behind it exits. Entries
have been observed 269 seconds stale with no process left running, so presence
alone would refuse to run long after the queue was actually clear.

It runs the same request twice. The first pass is a normal run and becomes the
baseline. The second pass waits until the fan out is genuinely partial, with at
least one specialist finished and at least one still running, then sends
`SIGKILL` to the worker, restarts it, and lets the workflow finish. Then it
prints the activity execution counts side by side.

Because every activity execution appends an `ActivityTaskScheduled` event, and
replay does not append new ones, those counts are the true number of times each
activity body ran. Matching counts mean nothing was recomputed:

```
  activity                            normal  crashed
  duplicate-detection.call_model           2        2   same
  policy-compliance.call_model             1        1   same
  vendor-risk.call_model                   3        3   same
  ...
```

Any agent that was mid-call when the process died may show one extra execution.
That is the honest and expected result: Temporal retried the work that was
genuinely in flight and nothing else.

### Doing it by hand instead

The script only automates the timing and the arithmetic. Killing the worker
yourself proves the same thing and is more visceral in front of a room, at the
cost of having to make the point yourself afterwards.

**Set the agent timeout low before you start, or you will stand in silence.** A
crashed worker reports nothing, so Temporal cannot know an in-flight activity is
orphaned until that activity's `start_to_close_timeout` expires. At the default
of 60 seconds a kill landing inside a model turn stalls the workflow for about
that long, and at the old 120 second default it measured 130.8 seconds of dead
air. `scripts/crash_demo.sh` sets this itself, which is why it carries no such
warning.

The whole sequence, in three terminals:

```bash
# 1. worker, with a short agent timeout so the recovery is quick
AGENT_ACTIVITY_TIMEOUT_SECONDS=15 ./scripts/worker.sh

# 2. API and UI
./scripts/api.sh

# 3. nothing yet, this is where you will kill the worker
```

Open http://127.0.0.1:8000 and start any scenario. The moment the three
specialists light up inside the dashed "concurrent" box, in the third terminal:

```bash
./scripts/cleanup.sh --all --worker --kill
```

That sends `SIGKILL` to the worker and leaves the API and UI running so you can
watch. Do not reach for `pkill -f "python worker.py"`: under `uv` the process
shows up as `.../MacOS/Python worker.py` with a capital P, so that pattern
matches nothing and exits quietly having done nothing at all.

The UI freezes and shows: "No worker is answering. Showing the last known
state. The workflow and everything the agents have already finished are safe on
the Temporal server, and will resume when a worker comes back." The finished
agents keep their findings, their latencies, and their token counts, because
that state lives in workflow history on the server rather than in the process
you just destroyed. Then bring the worker back:

```bash
AGENT_ACTIVITY_TIMEOUT_SECONDS=15 ./scripts/worker.sh
```

Within about 25 seconds the workflow picks up where it left off and runs to
completion. The specialists that had already reported are not re-run, and their
latencies and token counts in the telemetry table are the original ones, not
new numbers from a second call.

Two caveats.

**Use `kill -9`, not Ctrl+C.** Ctrl+C is a graceful shutdown: the worker takes
about four seconds to drain, reports its in-flight activities as failed on the
way out, and Temporal reschedules them immediately, so the workflow resumed in
14 seconds in testing. That still shows finished agent work surviving, but it
is a clean shutdown, and a skeptic can fairly say you asked the process to
stop. `kill -9` is the real thing: the process is gone with nothing reported.

**`cleanup.sh` is scoped to this checkout**, so a `worker.py` belonging to
another project on the same machine is never a target. `--worker` leaves the
API alone, and `--kill` skips the graceful `SIGTERM` that the script otherwise
sends. Without `--all` it would also skip the worker you started in a terminal,
since that one has a tty.

Model turns run one and a half to six seconds in practice, so an agent timeout
of 15 leaves ample headroom. The default is 60 because a demo timeout is not a
production one.

## Driving it from the command line instead

The UI is not required. To run a scenario without a browser:

```bash
./scripts/run_scenario.sh happy_path
./scripts/run_scenario.sh human_approval --decision approve
./scripts/run_scenario.sh human_rejection --decision reject
./scripts/run_scenario.sh erp_retry
```

That wrapper is `uv run python scripts/run_scenario.py`, which works too.

The workflow also accepts a Signal named `decide`, in addition to the
`submit_decision` Update, so you can drive the approval step with the Temporal
CLI:

```bash
temporal workflow signal --workflow-id po-PO-XXXXXXXX --name decide \
  --input '{"approved": true, "decided_by": "cli", "comment": "ok"}'
```

## The wrapper scripts

Each one changes to the repo root first, so it works from any directory, passes
its arguments through, and `exec`s the real command so exit codes propagate.

| Script | Wraps |
|---|---|
| `scripts/check_bedrock.sh` | `uv run python scripts/check_bedrock.py` |
| `scripts/worker.sh` | `uv run python worker.py` |
| `scripts/api.sh` | `uv run uvicorn api.main:app --reload`, with the host and port from `API_HOST` and `API_PORT` |
| `scripts/run_scenario.sh` | `uv run python scripts/run_scenario.py` |
| `scripts/crash_demo.sh` | `uv run python -u scripts/crash_demo.py`, the durability demo |
| `scripts/cleanup.sh` | stops this repo's worker and API processes, and can `SIGKILL` just the worker for the crash demo |

## Stopping things

`Ctrl+C` in each terminal is the normal way. `scripts/cleanup.sh` is for when
something is left running in the background and you cannot find it, usually
showing up as `[Errno 48] Address already in use` on the next start.

```bash
./scripts/cleanup.sh              # stop orphaned worker and API processes
./scripts/cleanup.sh --dry-run    # list what it would stop, kill nothing
./scripts/cleanup.sh --all        # also stop ones you started in a terminal
./scripts/cleanup.sh --worker     # only the worker, leave the API running
./scripts/cleanup.sh --kill       # SIGKILL, no graceful SIGTERM first
```

By default it only stops orphans: processes reparented to PID 1 with no
controlling terminal, which is what a background process looks like once its
shell is gone. A worker you started yourself is left alone, and the script says
so, unless you pass `--all`.

It is scoped to this checkout. A candidate has to have its working directory
set to this repo root, so a `worker.py` belonging to some other project on the
same machine is never a target. It stops the whole process tree, including the
uvicorn reload supervisor's children, and escalates from `SIGTERM` to
`SIGKILL` only if something refuses to exit.

**It never touches the Temporal server.** Start and stop that yourself.

## Configuration

Everything tunable is an environment variable, documented in `.env.example`:
the Temporal address and task queue, the Bedrock region and model, the approval
threshold, the hard block threshold, the blocked vendor list, the seeded ERP
failure count, the ERP retry ceiling, and the approval deadline.

Values that steer workflow control flow, such as the retry ceiling and the
approval deadline, are read once when a workflow starts and carried in its
input rather than read inside the workflow. That keeps history replay
deterministic even if a worker restarts with different variables set.

## The data the agents read

Three small files under `data/` stand in for systems that would live in an ERP:

- `vendors.json` is the vendor master: approval status, contract and tax id on
  file, years active, and an incident history.
- `po_history.json` is the recent purchase order history the duplicate
  detection agent searches.
- `procurement_policy.md` is the written policy the compliance agent reads.

Edit any of them and the agents' behavior changes on the next run with no code
change. Adding a high severity incident to an approved vendor, or a new clause
to the policy, is the quickest way to show an audience that the agents are
genuinely reading rather than pattern matching on the request text.

## Running each agent on a different model

Every agent falls back to `BEDROCK_MODEL_ID`, and each can be overridden:

```bash
BEDROCK_MODEL_EXTRACTION=us.amazon.nova-lite-v1:0
BEDROCK_MODEL_VENDOR_RISK=us.amazon.nova-pro-v1:0
BEDROCK_MODEL_POLICY=us.amazon.nova-pro-v1:0
BEDROCK_MODEL_DUPLICATE=us.amazon.nova-lite-v1:0
BEDROCK_MODEL_SUPERVISOR=us.amazon.nova-pro-v1:0
```

The findings panel shows which model answered for each agent, so a mixed
configuration is visible in the UI rather than buried in config.

## Tests

```bash
uv run pytest
```

The suite covers the guardrail policy boundaries, the ERP activity's behavior
at each Temporal attempt number, the agent tools, every agent's finding
mapping, the loop safeguards that force an agent to conclude, live Bedrock
extraction and live agent tool loops, and the scenarios end to end against a
real Temporal server.

Three of those e2e tests assert the claims this demo makes out loud rather than
taking them on trust: that the fan out finishes in well under the combined time
of its three agents, that the deterministic guardrail outranks the supervisor,
and that the agents escalate a request the threshold alone would have passed.

Tests that need Bedrock or a Temporal server are skipped, with the reason
printed, when those are not available. They are never backed by a fake model
response. The whole point of the demo is that the model call is real, so a
passing test suite that stubbed it out would be worse than a skipped one.

## Notes on the design

- The workflow's `status` query is the single source of truth for the UI. The
  API holds no state, the browser holds no model of the pipeline, and so the
  diagram, the agent findings, and the telemetry feed cannot drift from what
  the workflow actually did.
- The agents are the probabilistic components and are kept separable from the
  rules. The deterministic guardrail runs last and outranks the supervisor, so
  every hard block stays auditable and gives the same answer every time.
- Each agent is its own graph rather than one large graph, because the
  orchestrator workflow is what fans them out. That keeps the concurrency, the
  retries, and the durability of partial results in Temporal.
- An agent concludes by calling a `submit_finding` tool whose schema is its
  finding. That avoids a second round trip just to format the answer. On its
  final permitted turn the data tools are withheld, so an agent that would
  otherwise loop is forced to conclude instead of failing the workflow.
- The extraction graph has two nodes: the Bedrock call runs as an Activity
  because it does I/O and needs retries and timeouts, and the normalization
  step runs on the workflow thread because it is pure and cheap. Both
  execution locations are exercised on every run.
- The Temporal LangGraph plugin is in Public Preview.

## Gotchas worth knowing

Three things cost real time to find while building this, all specific to
running LangGraph under the Temporal plugin.

**Worker plugins are inherited from the Client.** Since Temporal Python SDK
1.32, passing the same `LangGraphPlugin` to both the Client and the Worker,
which older examples show, registers each graph's node activities twice and the
Worker refuses to start with `More than one activity named
po-extraction.extract_po`. Register it on the Client only.

**Node functions must live at module level.** The plugin identifies each node
by module and qualified name, so a node produced by a factory raises
`Cannot identify task ...: closures/local functions are not supported`. That is
why every agent module declares its own two-line `call_model` and `run_tools`
over the shared logic in `agents/react_agent.py`. The upside is that each agent
gets a distinct activity name in the Temporal UI, which matters when five
agents are running.

**Conditional edge routers must be async.** LangGraph awaits an async branch
function directly but offloads a sync one through `loop.run_in_executor`, and a
Temporal workflow's event loop raises `NotImplementedError` for that on purpose,
since running workflow logic on a thread pool would be non-deterministic. A
sync router fails the workflow task with a bare `NotImplementedError` from
`asyncio/events.py` and no useful context.
