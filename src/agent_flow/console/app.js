import { requestJson, setToken, clearToken } from "./api.js";
import { createState, mergeEvents, toggleNode, selectTrace, isTerminal } from "./state.js";
import { renderTraceList, renderWorkspace, showAlert } from "./render.js";

const state = createState();
let eventTimer = null;
let listTimer = null;
let submissionActive = false;

const LIST_REFRESH_MS = 5000;

const dom = {};

function cacheDom() {
  for (const id of [
    "token-dialog", "token-form", "token-input", "retry-button", "retry-dialog",
    "retry-form", "retry-reason", "logout-button", "simulator-toggle",
    "simulator-panel", "simulator-form", "sim-message", "simulator-transcript",
    "trace-list", "trace-workspace",
  ]) {
    dom[id] = document.getElementById(id);
  }
}

function uuid() {
  return crypto.randomUUID();
}

// --- authentication -------------------------------------------------------

function requireAuth() {
  dom["token-dialog"].showModal();
}

async function onTokenSubmit(event) {
  event.preventDefault();
  const value = dom["token-input"].value;
  if (!value.trim()) return;
  setToken(value);
  dom["token-input"].value = "";
  state.authenticated = true;
  dom["token-dialog"].close();
  dom["logout-button"].hidden = false;
  await refreshTraces();
  startListRefresh();
}

function logout() {
  clearToken();
  state.authenticated = false;
  stopEventPolling();
  stopListRefresh();
  requireAuth();
}

// Keep the trace list current while the operator watches; selection and event
// polling are untouched by a list refresh.
function startListRefresh() {
  stopListRefresh();
  listTimer = setInterval(() => {
    if (state.authenticated) refreshTraces();
  }, LIST_REFRESH_MS);
}

function stopListRefresh() {
  if (listTimer) clearInterval(listTimer);
  listTimer = null;
}

// --- trace list + workspace ----------------------------------------------

async function refreshTraces() {
  const { ok, body } = await requestJson("/api/v1/traces");
  if (!ok || !body) {
    showAlert("Unable to load traces.");
    return;
  }
  state.traces = body.items || [];
  renderTraceList(dom["trace-list"], state, selectAndLoad);
  if (state.selectedTraceId === null && state.traces.length > 0) {
    await selectAndLoad(state.traces[0].trace_id);
  }
}

async function selectAndLoad(traceId) {
  stopEventPolling();
  const { ok, body } = await requestJson(`/api/v1/traces/${traceId}`);
  const trace = ok && body ? body : { trace_id: traceId, id: traceId, spans: [], events: [] };
  selectTrace(state, trace);
  state.selectedTraceId = traceId;
  renderTraceList(dom["trace-list"], state, selectAndLoad);
  renderWorkspace(dom["trace-workspace"], state, onToggleNode);
  updateRetryButton();
  startEventPolling();
}

function onToggleNode(key) {
  toggleNode(state, key);
  renderWorkspace(dom["trace-workspace"], state, onToggleNode);
  updateRetryButton();
}

function updateRetryButton() {
  const trace = state.selectedTrace;
  dom["retry-button"].hidden = !(trace && isTerminal(trace.status));
}

// --- bounded event polling ------------------------------------------------

const BACKOFF = [2000, 4000, 8000, 15000];

function stopEventPolling() {
  if (eventTimer) clearTimeout(eventTimer);
  eventTimer = null;
  state.polling.active = false;
}

function startEventPolling() {
  state.polling = { active: true, stale: false, failures: 0 };
  schedulePoll(0);
}

function schedulePoll(delay) {
  if (!state.polling.active) return;
  eventTimer = setTimeout(pollEventsOnce, delay);
}

async function pollEventsOnce() {
  if (!state.polling.active || state.selectedTraceId === null) return;
  const traceId = state.selectedTraceId;
  try {
    const { ok, body } = await requestJson(
      `/api/v1/traces/${traceId}/events?after_sequence=${state.eventCursor}`
    );
    if (!ok || !body) throw new Error("poll failed");
    if (traceId !== state.selectedTraceId) return;
    const fresh = mergeEvents(state, body.events || []);
    if (fresh.length > 0 && state.selectedTrace) {
      state.selectedTrace.events = state.events;
      renderWorkspace(dom["trace-workspace"], state, onToggleNode);
    }
    state.polling.failures = 0;
    const terminal = state.selectedTrace && isTerminal(state.selectedTrace.status);
    schedulePoll(terminal ? 5000 : 1000);
  } catch (error) {
    state.polling.stale = true;
    const delay = BACKOFF[Math.min(state.polling.failures, BACKOFF.length - 1)];
    state.polling.failures += 1;
    schedulePoll(delay);
  }
}

// --- manual retry ---------------------------------------------------------

function openRetry() {
  dom["retry-reason"].value = "";
  dom["retry-dialog"].showModal();
}

async function onRetrySubmit(event) {
  const submitter = event.submitter;
  if (!submitter || submitter.value !== "confirm") {
    dom["retry-dialog"].close();
    return;
  }
  event.preventDefault();
  const reason = dom["retry-reason"].value.trim();
  if (!reason || state.selectedTraceId === null) return;
  const source = state.selectedTraceId;
  const { status, body } = await requestJson(
    `/api/v1/traces/${source}/retry`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, idempotency_key: uuid() }),
    }
  );
  dom["retry-dialog"].close();
  if (status !== 202 || !body) {
    showAlert("Retry was not accepted.");
    return;
  }
  state.selectedTraceId = null;
  await refreshTraces();
  await selectAndLoad(body.trace_id);
}

// --- inbound simulator ----------------------------------------------------

function toggleSimulator() {
  const open = dom["simulator-panel"].hidden;
  dom["simulator-panel"].hidden = !open;
  dom["simulator-toggle"].setAttribute("aria-expanded", open ? "true" : "false");
  if (open && !state.simulator.sessionId) {
    state.simulator.sessionId = `console-${uuid()}`;
  }
}

async function onSimulatorSubmit(event) {
  event.preventDefault();
  const text = dom["sim-message"].value.trim();
  if (!text || submissionActive) return;
  const messageId = uuid();
  pushMessage("customer", text);
  const { status, body } = await requestJson("/api/v1/submissions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      channel: "console",
      external_message_id: messageId,
      session_id: state.simulator.sessionId,
      text,
      idempotency_key: messageId,
      metadata: { source: "trace-console" },
    }),
  });
  if (status !== 202 || !body) {
    showAlert("Submission was rejected.");
    return;
  }
  dom["sim-message"].value = "";
  selectTrace(state, { trace_id: body.trace_id, id: body.trace_id, spans: [], events: [] });
  state.selectedTraceId = body.trace_id;
  renderWorkspace(dom["trace-workspace"], state, onToggleNode);
  await refreshTraces();
  startEventPolling();
  await pollSubmission(body.submission_id);
}

const STATUS_LABEL = { queued: "佇列中", running: "處理中", failed: "處理失敗" };

// Chatroom transcript: {role: "customer"|"agent"|"status", text}. Only safe
// customer-facing text is ever shown here — never drafts or reasoning.
function pushMessage(role, text) {
  const log = state.simulator.messages || (state.simulator.messages = []);
  const last = log[log.length - 1];
  if (last && last.role === role && last.text === text) return;
  // Status is transient: replace a prior status rather than stacking them.
  if (role === "status" && last && last.role === "status") log.pop();
  log.push({ role, text });
  renderTranscript();
}

function renderTranscript() {
  const panel = dom["simulator-transcript"];
  panel.textContent = "";
  for (const message of state.simulator.messages || []) {
    const row = document.createElement("div");
    row.className = `chat-row chat-${message.role}`;
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = message.text;
    row.appendChild(bubble);
    panel.appendChild(row);
  }
  panel.scrollTop = panel.scrollHeight;
}

function transcriptMessage(body) {
  if (body.status === "completed") {
    if (body.handoff && body.handoff.safe_message) {
      return { role: "agent", text: body.handoff.safe_message };
    }
    return { role: "agent", text: body.text || "已完成" };
  }
  return { role: "status", text: STATUS_LABEL[body.status] || body.status };
}

async function pollSubmission(submissionId) {
  submissionActive = true;
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const { ok, body } = await requestJson(`/api/v1/submissions/${submissionId}`);
      if (!ok || !body) break;
      const message = transcriptMessage(body);
      pushMessage(message.role, message.text);
      if (body.status === "completed" || body.status === "failed") break;
      await sleep(300);
    }
  } finally {
    submissionActive = false;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- bootstrap ------------------------------------------------------------

function init() {
  cacheDom();
  dom["token-form"].addEventListener("submit", onTokenSubmit);
  dom["logout-button"].addEventListener("click", logout);
  dom["retry-button"].addEventListener("click", openRetry);
  dom["retry-form"].addEventListener("submit", onRetrySubmit);
  dom["simulator-toggle"].addEventListener("click", toggleSimulator);
  dom["simulator-form"].addEventListener("submit", onSimulatorSubmit);
  window.addEventListener("beforeunload", stopEventPolling);
  requireAuth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
