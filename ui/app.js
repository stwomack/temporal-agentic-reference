"use strict";

// Single page UI for the durable PO approval demo.
//
// Everything on screen is derived from one server sent event stream, which in
// turn is the workflow's own `status` query plus DescribeWorkflowExecution.
// There is no client side model of the pipeline, so the diagram, the telemetry
// feed, and the code panel cannot disagree with the workflow.

const el = (id) => document.getElementById(id);

// Populated from /api/steps so the UI never hardcodes which steps are agents.
const AGENT_STEP_NAMES = new Set();

const state = {
  steps: [],
  scenarios: [],
  workflowId: null,
  source: null,
  sourceStep: null,
  eventSource: null,
  decisionInFlight: false,
};

const TERMINAL_STATES = {
  submitted: { cls: "good", text: "Submitted to ERP" },
  rejected_by_policy: { cls: "bad", text: "Rejected by the deterministic guardrail" },
  rejected_by_supervisor: { cls: "bad", text: "Rejected by the supervisor agent" },
  rejected_by_human: { cls: "bad", text: "Rejected by human reviewer" },
  approval_timed_out: { cls: "warn", text: "Approval deadline passed" },
  failed: { cls: "bad", text: "Workflow failed" },
};

function showError(message) {
  const banner = el("error-banner");
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch (err) {
    body = null;
  }
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : text || response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return body;
}

function money(amount, currency) {
  const value = Number(amount || 0);
  return `${currency || "USD"} ${value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

// ------------------------------------------------------------------ bootstrap

async function loadStatic() {
  try {
    const [health, steps, scenarios] = await Promise.all([
      getJSON("/api/health"),
      getJSON("/api/steps"),
      getJSON("/api/scenarios"),
    ]);
    el("pill-temporal").textContent =
      `temporal: ${health.temporal_address} / ${health.temporal_namespace}` +
      (health.temporal_tls ? " (tls)" : "");
    el("pill-model").textContent = `bedrock: ${health.bedrock_model_id}`;
    if (health.temporal_ui_url) {
      el("link-temporal-ui").href = health.temporal_ui_url;
    }
    state.steps = steps;
    state.scenarios = scenarios;
    for (const column of steps) {
      for (const step of column) {
        if (step.is_agent) {
          AGENT_STEP_NAMES.add(step.name);
        }
      }
    }
    renderScenarios();
    renderDiagram(null);
  } catch (err) {
    showError(`Could not load configuration from the API. ${err.message}`);
  }
}

function renderScenarios() {
  const host = el("scenarios");
  host.textContent = "";
  for (const spec of state.scenarios) {
    const button = document.createElement("button");
    button.className = "scenario";
    button.dataset.scenario = spec.scenario;
    const title = document.createElement("strong");
    title.textContent = spec.title;
    const description = document.createElement("span");
    description.textContent = spec.description;
    button.append(title, description);
    button.addEventListener("click", () => startScenario(spec.scenario));
    host.appendChild(button);
  }
}

function setScenarioButtonsDisabled(disabled) {
  for (const button of document.querySelectorAll(".scenario")) {
    button.disabled = disabled;
  }
}

// ---------------------------------------------------------------------- start

async function startScenario(scenario) {
  showError(null);
  setScenarioButtonsDisabled(true);
  resetView();
  try {
    const started = await getJSON("/api/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario }),
    });
    state.workflowId = started.workflow_id;
    el("run-label").textContent = `workflow ${started.workflow_id}`;
    const link = el("run-link");
    link.href = started.temporal_ui_url;
    link.hidden = false;
    subscribe(started.workflow_id);
  } catch (err) {
    showError(`Could not start the workflow. ${err.message}`);
    setScenarioButtonsDisabled(false);
  }
}

function resetView() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  state.workflowId = null;
  state.source = null;
  state.sourceStep = null;
  el("run-link").hidden = true;
  el("run-label").textContent = "starting";
  el("approval").hidden = true;
  el("outcome").hidden = true;
  el("pending").hidden = true;
  el("extracted").className = "extracted empty";
  el("extracted").textContent = "Nothing extracted yet.";
  el("findings").className = "findings empty";
  el("findings").textContent = "No agent has reported yet.";
  el("feed").tBodies[0].innerHTML =
    '<tr class="empty-row"><td colspan="5">No events yet.</td></tr>';
  el("code-meta").textContent = "Start a scenario to follow the executing code.";
  el("code").firstElementChild.textContent = "";
  renderDiagram(null);
}

function subscribe(workflowId) {
  const source = new EventSource(`/api/workflows/${encodeURIComponent(workflowId)}/stream`);
  state.eventSource = source;

  source.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (err) {
      showError(`Malformed update from the API. ${err.message}`);
    }
  };
  source.addEventListener("error", (event) => {
    if (event.data) {
      try {
        showError(`Stream error. ${JSON.parse(event.data).detail}`);
      } catch (err) {
        showError("Stream error.");
      }
    }
  });
  source.addEventListener("done", () => {
    source.close();
    state.eventSource = null;
    setScenarioButtonsDisabled(false);
  });
  source.onerror = () => {
    // A closed stream after completion is normal. Only report while running.
    if (state.eventSource) {
      showError("Lost the event stream. The workflow may still be running.");
      setScenarioButtonsDisabled(false);
    }
  };
}

// --------------------------------------------------------------------- render

function render(payload) {
  const status = payload.status;
  renderDiagram(status);
  renderFindings(status);
  renderFeed(status);
  renderPending(payload.pending_activities || []);
  renderExtracted(status);
  renderApproval(status);
  renderOutcome(payload, status);
  if (status) {
    syncCodePanel(status);
  }
}

function renderDiagram(status) {
  const host = el("diagram");
  host.textContent = "";
  const byName = {};
  if (status) {
    for (const step of status.steps || []) {
      byName[step.name] = step;
    }
  }

  state.steps.forEach((column, index) => {
    const columnEl = document.createElement("div");
    columnEl.className = column.length > 1 ? "column fanout" : "column";

    for (const definition of column) {
      const step = byName[definition.name];
      const node = document.createElement("div");
      const statusName = step ? step.status : "pending";
      node.className = `node ${statusName}`;

      const label = document.createElement("div");
      label.className = "node-label";
      label.textContent = definition.label;
      if (definition.is_agent) {
        const badge = document.createElement("span");
        badge.className = "agent-badge";
        badge.textContent = "agent";
        label.appendChild(badge);
      }

      const statusLine = document.createElement("div");
      statusLine.className = "node-status";
      let statusText = statusName;
      if (step && step.latency_ms !== null && step.latency_ms !== undefined) {
        statusText += ` (${step.latency_ms} ms)`;
      }
      if (step && step.attempts > 1) {
        statusText += ` after ${step.attempts} attempts`;
      }
      statusLine.textContent = statusText;

      node.append(label, statusLine);

      if (step && step.detail) {
        const detail = document.createElement("div");
        detail.className = "node-detail";
        detail.textContent = step.detail;
        node.appendChild(detail);
      }
      columnEl.appendChild(node);
    }

    host.appendChild(columnEl);
    if (index < state.steps.length - 1) {
      const arrow = document.createElement("div");
      arrow.className = "arrow";
      arrow.textContent = "\u2192";
      host.appendChild(arrow);
    }
  });
}

const SEVERITY_ORDER = { blocker: 0, caution: 1, ok: 2 };

function renderFindings(status) {
  const host = el("findings");
  const findings = (status && status.agent_findings) || [];
  const running = (status && status.steps ? status.steps : []).filter(
    (step) => step.status === "running" && AGENT_STEP_NAMES.has(step.name)
  );

  if (findings.length === 0 && running.length === 0) {
    host.className = "findings empty";
    host.textContent = "No agent has reported yet.";
    return;
  }

  host.className = "findings";
  host.textContent = "";

  for (const finding of findings) {
    const card = document.createElement("div");
    card.className = `finding ${finding.severity}`;

    const title = document.createElement("h3");
    title.textContent = finding.label;

    const headline = document.createElement("div");
    headline.className = "headline";
    headline.textContent = finding.headline;

    const rationale = document.createElement("div");
    rationale.className = "rationale";
    rationale.textContent = finding.detail;

    const meta = document.createElement("div");
    meta.className = "meta";
    const tools = finding.tool_calls.length ? finding.tool_calls.join(", ") : "none";
    meta.textContent =
      `${finding.model_id}\n` +
      `${finding.turns} turn(s), tools: ${tools}\n` +
      `${finding.input_tokens} in / ${finding.output_tokens} out tokens`;

    card.append(title, headline, rationale, meta);
    host.appendChild(card);
  }

  // Show a placeholder for an agent that is still working, so the panel fills
  // in progressively during the fan out instead of appearing all at once.
  const reported = new Set(findings.map((f) => f.agent));
  for (const step of running) {
    if (reported.has(step.name)) {
      continue;
    }
    const card = document.createElement("div");
    card.className = "finding running";
    const title = document.createElement("h3");
    title.textContent = step.label;
    const headline = document.createElement("div");
    headline.className = "headline";
    headline.textContent = "Running";
    card.append(title, headline);
    host.appendChild(card);
  }
}

function renderFeed(status) {
  const body = el("feed").tBodies[0];
  if (!status) {
    return;
  }
  const rows = (status.steps || []).filter(
    (step) => step.status !== "pending"
  );
  if (rows.length === 0) {
    body.innerHTML = '<tr class="empty-row"><td colspan="5">No events yet.</td></tr>';
    return;
  }
  body.textContent = "";
  for (const step of rows) {
    const tr = document.createElement("tr");

    const name = document.createElement("td");
    name.textContent = step.label;

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `st ${step.status}`;
    badge.textContent = step.status;
    statusCell.appendChild(badge);

    const latency = document.createElement("td");
    latency.className = "num";
    latency.textContent =
      step.latency_ms === null || step.latency_ms === undefined
        ? "-"
        : `${step.latency_ms} ms`;

    const when = document.createElement("td");
    const stamp = step.finished_at || step.started_at;
    when.textContent = stamp ? new Date(stamp).toLocaleTimeString() : "-";

    const detail = document.createElement("td");
    detail.className = "detail";
    detail.textContent = step.detail || "";

    tr.append(name, statusCell, latency, when, detail);
    body.appendChild(tr);
  }
}

function renderPending(pendingActivities) {
  // Only worth a banner once an activity is on its second attempt or later.
  // A first attempt in flight is just normal progress, already shown in the
  // diagram, and calling that a retry would be wrong.
  const host = el("pending");
  const retrying = pendingActivities.filter((activity) => activity.attempt > 1);
  if (retrying.length === 0) {
    host.hidden = true;
    host.textContent = "";
    return;
  }
  const lines = retrying.map((activity) => {
    const max = activity.maximum_attempts ? ` of ${activity.maximum_attempts}` : "";
    const failure = activity.last_failure ? ` Last failure: ${activity.last_failure}` : "";
    return `${activity.activity_type} is on attempt ${activity.attempt}${max}.${failure}`;
  });
  host.hidden = false;
  host.textContent = `Temporal is retrying. ${lines.join(" ")}`;
}

function renderExtracted(status) {
  const host = el("extracted");
  const extracted = status && status.extracted;
  if (!extracted) {
    host.className = "extracted empty";
    host.textContent = "Nothing extracted yet.";
    return;
  }
  host.className = "extracted";
  host.textContent = "";

  const list = document.createElement("dl");
  list.className = "kv";
  const pairs = [
    ["Vendor", extracted.vendor || "(not stated)"],
    ["Requester", extracted.requester || "(not stated)"],
    ["Total", money(extracted.total_amount, extracted.currency)],
    ["Model", extracted.model_id],
    ["Tokens", `${extracted.input_tokens} in / ${extracted.output_tokens} out`],
  ];
  if (extracted.notes) {
    pairs.push(["Notes", extracted.notes]);
  }
  for (const [key, value] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  host.appendChild(list);

  if ((extracted.line_items || []).length > 0) {
    const table = document.createElement("table");
    table.className = "items";
    const head = document.createElement("thead");
    head.innerHTML =
      '<tr><th>Description</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Amount</th></tr>';
    const tbody = document.createElement("tbody");
    for (const item of extracted.line_items) {
      const tr = document.createElement("tr");
      const description = document.createElement("td");
      description.textContent = item.description;
      const quantity = document.createElement("td");
      quantity.className = "num";
      quantity.textContent = Number(item.quantity).toLocaleString();
      const unit = document.createElement("td");
      unit.className = "num";
      unit.textContent = Number(item.unit_price).toFixed(2);
      const amount = document.createElement("td");
      amount.className = "num";
      amount.textContent = Number(item.amount).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
      tr.append(description, quantity, unit, amount);
      tbody.appendChild(tr);
    }
    table.append(head, tbody);
    host.appendChild(table);
  }
}

function renderApproval(status) {
  const card = el("approval");
  const awaiting = status && status.state === "awaiting_approval";
  card.hidden = !awaiting;
  if (!awaiting) {
    return;
  }
  // The workflow already composed the reason, which may be a supervisor
  // escalation, a threshold breach, or both. Reading the guardrail alone would
  // print "no approval needed" on an agent driven escalation.
  const approvalStep = (status.steps || []).find((step) => step.name === "approval");
  el("approval-reason").textContent =
    (approvalStep && approvalStep.detail) ||
    (status.guardrail && status.guardrail.reason) ||
    "A decision is required.";
  el("btn-approve").disabled = state.decisionInFlight;
  el("btn-reject").disabled = state.decisionInFlight;
}

function renderOutcome(payload, status) {
  const host = el("outcome");
  const stateName = status ? status.state : null;
  const terminal = stateName ? TERMINAL_STATES[stateName] : null;
  if (!terminal) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.className = `outcome ${terminal.cls}`;
  let text = terminal.text;
  if (status.erp && status.erp.confirmation_id) {
    text += `. Confirmation ${status.erp.confirmation_id} on attempt ${status.erp.attempts_used}`;
  }
  if (status.guardrail && status.guardrail.blocked) {
    text += `. ${status.guardrail.reason}`;
  }
  if (status.supervisor && stateName !== "rejected_by_policy") {
    text += `. Supervisor: ${status.supervisor.recommendation}`;
  }
  if (status.decision) {
    text += `. Decision by ${status.decision.decided_by}`;
    if (status.decision.comment) {
      text += `: ${status.decision.comment}`;
    }
  }
  host.textContent = text;
}

// ----------------------------------------------------------------- decisions

async function sendDecision(approved) {
  if (!state.workflowId || state.decisionInFlight) {
    return;
  }
  state.decisionInFlight = true;
  el("btn-approve").disabled = true;
  el("btn-reject").disabled = true;
  showError(null);
  try {
    await getJSON(`/api/workflows/${encodeURIComponent(state.workflowId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approved,
        decided_by: "demo-operator",
        comment: el("approval-comment").value || "",
      }),
    });
    el("approval-comment").value = "";
  } catch (err) {
    showError(`The decision was not accepted. ${err.message}`);
  } finally {
    state.decisionInFlight = false;
  }
}

// ---------------------------------------------------------------- code panel

function activeStepName(status) {
  if (status.current_step) {
    return status.current_step;
  }
  // Once the workflow ends, keep the last step that actually ran on screen.
  const ran = (status.steps || []).filter(
    (step) => step.status === "completed" || step.status === "failed"
  );
  return ran.length > 0 ? ran[ran.length - 1].name : null;
}

async function syncCodePanel(status) {
  const step = activeStepName(status);
  if (!step || step === state.sourceStep) {
    return;
  }
  state.sourceStep = step;
  try {
    const source = await getJSON(`/api/source/${encodeURIComponent(step)}`);
    if (state.sourceStep !== step) {
      return; // A newer step won the race.
    }
    state.source = source;
    paintCode(source);
  } catch (err) {
    el("code-meta").textContent = `Could not load source for ${step}. ${err.message}`;
  }
}

function paintCode(source) {
  el("code-meta").textContent = `${source.file} : ${source.symbol} lines ${source.start_line}-${source.end_line}`;
  const host = el("code").firstElementChild;
  host.textContent = "";
  let firstHighlighted = null;

  source.code.split("\n").forEach((text, index) => {
    const number = index + 1;
    const line = document.createElement("div");
    const highlighted = number >= source.start_line && number <= source.end_line;
    line.className = highlighted ? "ln hl" : "ln";
    if (highlighted && firstHighlighted === null) {
      line.classList.add("first-hl");
      firstHighlighted = line;
    }
    const no = document.createElement("span");
    no.className = "no";
    no.textContent = String(number);
    const tx = document.createElement("span");
    tx.className = "tx";
    tx.textContent = text;
    line.append(no, tx);
    host.appendChild(line);
  });

  if (firstHighlighted) {
    // Put the highlighted block near the top of the panel rather than dead
    // centre, so the whole function body is usually visible.
    const pre = el("code");
    pre.scrollTop = Math.max(0, firstHighlighted.offsetTop - pre.clientHeight * 0.2);
  }
}

// ------------------------------------------------------------------- wire up

el("btn-approve").addEventListener("click", () => sendDecision(true));
el("btn-reject").addEventListener("click", () => sendDecision(false));
loadStatic();
