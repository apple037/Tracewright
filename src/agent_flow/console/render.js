// Allowlisted, injection-safe rendering. Dynamic data only via textContent.
import { nodeKey } from "./state.js";

const DETAIL_FIELDS = new Set([
  "decision_summary", "reason_codes", "model_role", "model_profile",
  "duration_ms", "input_tokens", "output_tokens", "tool",
  "freshness_seconds", "attempt", "delivery_disposition",
  "error_code", "failure_stage", "component", "operation",
  "artifact_id", "semantic_version", "checksum", "sequence", "created_at",
  "intent", "emotion_category",
]);

let panelCounter = 0;

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child) node.appendChild(child);
  }
  return node;
}

export function showAlert(message) {
  const region = document.getElementById("alert-region");
  region.textContent = "";
  if (message) region.appendChild(el("p", { text: message }));
}

function traceLabel(trace) {
  return trace.external_message_id || trace.trace_id || "trace";
}

export function renderTraceList(container, state, onSelect) {
  container.textContent = "";
  if (state.traces.length === 0) {
    container.appendChild(el("p", { text: "No traces yet." }));
    return;
  }
  for (const trace of state.traces) {
    const selected = trace.trace_id === state.selectedTraceId;
    const button = el("button", {
      type: "button",
      class: "trace-item",
      "aria-label": traceLabel(trace),
      "aria-pressed": selected ? "true" : "false",
      dataset: { traceId: trace.trace_id },
    });
    button.appendChild(el("span", { text: traceLabel(trace) }));
    button.appendChild(statusBadge(trace.status));
    button.addEventListener("click", () => onSelect(trace.trace_id));
    container.appendChild(button);
  }
}

function statusBadge(status) {
  const icon = { succeeded: "✓", completed: "✓", running: "…", queued: "…", failed: "✗" };
  return el("span", {
    class: `status status-${status}`,
    text: `${icon[status] || "•"} ${status}`,
  });
}

function detailPanel(fields, issue, node) {
  const panel = el("div", { class: "node-detail" });
  const merged = { ...fields };
  if (issue && issue.failed_node === node) {
    merged.error_code = issue.error_code;
    merged.component = issue.component;
    merged.operation = issue.operation;
  }
  let rendered = 0;
  for (const [key, value] of Object.entries(merged)) {
    if (!DETAIL_FIELDS.has(key) || value === null || value === undefined) continue;
    rendered += 1;
    const text = Array.isArray(value) ? value.slice(0, 20).join(", ") : String(value).slice(0, 256);
    const span = el("span", { class: "detail-field", text });
    span.setAttribute(`data-${key.replace(/_/g, "-")}`, "");
    panel.appendChild(span);
  }
  if (rendered === 0) {
    panel.appendChild(el("span", { text: "Additional safe metadata unavailable" }));
  }
  return panel;
}

function nodeSummaryFields(trace, node) {
  // Fold allowlisted fields from this node's completed/telemetry events.
  const fields = {};
  for (const event of trace.events || []) {
    if ((event.payload && event.payload.node) !== node) continue;
    const payload = event.payload || {};
    for (const key of DETAIL_FIELDS) {
      if (payload[key] !== undefined) fields[key] = payload[key];
    }
    if (event.error_code) fields.error_code = event.error_code;
  }
  return fields;
}

// A safe, ordered narrative of the agent's decisions — the "thinking process"
// summary for operators. Every line is an allowlisted field; no raw model
// reasoning or chain-of-thought is ever stored or shown.
function reasoningLine(payload) {
  const parts = [];
  if (payload.decision_summary) parts.push(String(payload.decision_summary));
  if (payload.intent) parts.push(`intent: ${payload.intent}`);
  if (payload.emotion_category) parts.push(`emotion: ${payload.emotion_category}`);
  if (Array.isArray(payload.reason_codes) && payload.reason_codes.length) {
    parts.push(payload.reason_codes.slice(0, 8).join(", "));
  }
  if (Array.isArray(payload.evidence_ids) && payload.evidence_ids.length) {
    parts.push(`${payload.evidence_ids.length} evidence linked`);
  }
  if (payload.model_role) {
    const timing = payload.duration_ms != null ? ` · ${payload.duration_ms} ms` : "";
    const tokens = payload.output_tokens != null ? ` · ${payload.output_tokens} out-tok` : "";
    parts.push(`model ${payload.model_role}${timing}${tokens}`);
  }
  return parts.join(" — ");
}

function renderReasoningTrail(trace) {
  const rows = [];
  for (const event of trace.events || []) {
    const payload = event.payload || {};
    const node = payload.node;
    if (!node) continue;
    if (event.event_type === "node" && (payload.lifecycle_id || "").endsWith(":started")) continue;
    const status = event.status || "";
    if (event.event_type === "node" && status !== "completed" && status !== "failed") continue;
    const summary = reasoningLine(payload);
    if (!summary && status !== "failed") continue;
    rows.push({ node, status, summary });
  }
  if (rows.length === 0) return null;

  const details = el("details", { class: "reasoning-trail", open: "" });
  details.appendChild(el("summary", { text: `Reasoning summary · ${rows.length} steps` }));
  const list = el("ol", { class: "reasoning-steps" });
  for (const row of rows) {
    const li = el("li", { class: `reasoning-step reasoning-${row.status}`, dataset: { node: row.node } });
    li.appendChild(el("span", { class: "reasoning-node", text: row.node }));
    li.appendChild(el("span", { class: "reasoning-detail", text: row.summary || row.status }));
    list.appendChild(li);
  }
  details.appendChild(list);
  return details;
}

export function renderWorkspace(container, state, onToggle) {
  container.textContent = "";
  const trace = state.selectedTrace;
  if (!trace) {
    container.appendChild(el("p", { text: "Select a trace to inspect its flow." }));
    return;
  }
  container.appendChild(el("p", { class: "workspace-heading", dataset: { selectedTrace: "" }, text: traceLabel(trace) }));
  const trail = renderReasoningTrail(trace);
  if (trail) container.appendChild(trail);
  const flow = el("div", { class: "node-flow" });
  for (const span of trace.spans || []) {
    const node = span.node || span.name;
    const key = nodeKey(span);
    const expanded = state.expandedNodes.has(key);
    panelCounter += 1;
    const panelId = `node-panel-${panelCounter}`;
    const item = el("div", {
      class: `node node-${span.status}`,
      role: "button",
      tabindex: "0",
      "aria-expanded": expanded ? "true" : "false",
      "aria-controls": panelId,
      dataset: { node, status: span.status, attempt: span.attempt || 1 },
    });
    item.appendChild(el("strong", { text: node }));
    item.appendChild(statusBadge(span.status));
    const panel = detailPanel(nodeSummaryFields(trace, node), trace.issue_summary, node);
    panel.id = panelId;
    panel.hidden = !expanded;
    item.appendChild(panel);
    const toggle = () => onToggle(key);
    item.addEventListener("click", toggle);
    item.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter" || evt.key === " ") {
        evt.preventDefault();
        toggle();
      }
    });
    flow.appendChild(item);
  }
  container.appendChild(flow);
}
