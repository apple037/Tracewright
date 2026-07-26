import { requestJson, setToken, clearToken } from "./api.js";
import { createState, mergeEvents, toggleNode, selectTrace, isTerminal } from "./state.js";
import { renderTraceList, renderWorkspace, showAlert } from "./render.js";
import { t, applyStatic, toggleLang } from "./i18n.js";
import { createChatroom } from "./chatroom.js";
import { createTunePanel } from "./tune.js";

const state = createState();
let eventTimer = null;
let listTimer = null;
let chatroom = null;
let tunePanel = null;

const LIST_REFRESH_MS = 5000;
const THEME_KEY = "tracewright.theme";

const dom = {};

function cacheDom() {
  for (const id of [
    "token-dialog", "token-form", "token-input", "retry-button", "retry-dialog",
    "retry-form", "retry-reason", "logout-button", "lang-toggle", "theme-toggle",
    "tune-button", "tune-dialog", "tune-body", "tune-close",
    "trace-list", "trace-items", "trace-workspace", "workspace-body",
    "chat-transcript", "chat-form", "chat-input", "chat-send", "chat-reset",
  ]) {
    dom[id] = document.getElementById(id);
  }
}

// --- theme ----------------------------------------------------------------

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}

function storedTheme() {
  try {
    return window.localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

function onToggleTheme() {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const current = document.documentElement.getAttribute("data-theme")
    || (prefersDark ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    window.localStorage.setItem(THEME_KEY, next);
  } catch {
    /* theme simply will not persist */
  }
}

// --- chat -----------------------------------------------------------------

// Sending a chat message from the demo panel selects its trace so the operator
// watches the pipeline run live in the same view — no page switch.
async function onChatTrace(traceId) {
  await refreshTraces();
  await selectAndLoad(traceId);
}

// Disabling send while a turn is in flight is what stops a second message
// being silently swallowed after the textarea has already been cleared.
function onChatBusy(busy) {
  dom["chat-send"].disabled = busy;
  dom["chat-input"].readOnly = busy;
  dom["chat-transcript"].setAttribute("aria-busy", busy ? "true" : "false");
}

async function onChatSubmit(event) {
  event.preventDefault();
  const text = dom["chat-input"].value.trim();
  if (!text || !chatroom || chatroom.sending()) return;
  dom["chat-input"].value = "";
  await chatroom.submit(text);
}

function onChatReset() {
  if (!chatroom || chatroom.sending()) return;
  chatroom.reset();
  dom["chat-input"].focus();
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
  dom["tune-button"].hidden = false;
  await chatroom.restore();
  await refreshTraces();
  startListRefresh();
  dom["chat-input"].focus();
}

function logout() {
  clearToken();
  state.authenticated = false;
  stopEventPolling();
  stopListRefresh();
  state.traces = [];
  state.selectedTrace = null;
  state.selectedTraceId = null;
  renderTraceList(dom["trace-items"], state, selectAndLoad);
  renderWorkspace(dom["workspace-body"], state, onToggleNode);
  dom["tune-button"].hidden = true;
  dom["retry-button"].hidden = true;
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
    showAlert(t("alert.tracesFailed"));
    return;
  }
  state.traces = body.items || [];
  renderTraceList(dom["trace-items"], state, selectAndLoad);
  if (state.selectedTraceId === null && state.traces.length > 0) {
    await selectAndLoad(state.traces[0].trace_id);
  }
}

async function selectAndLoad(traceId) {
  stopEventPolling();
  dom["workspace-body"].setAttribute("aria-busy", "true");
  const { ok, body } = await requestJson(`/api/v1/traces/${traceId}`);
  dom["workspace-body"].removeAttribute("aria-busy");
  // A failed fetch and a trace with no spans used to render identically; keep
  // them distinguishable so an operator is not debugging an empty pipeline.
  const trace = ok && body ? body : { trace_id: traceId, id: traceId, spans: [], events: [] };
  selectTrace(state, trace);
  state.loadFailed = !(ok && body);
  state.selectedTraceId = traceId;
  renderTraceList(dom["trace-items"], state, selectAndLoad);
  renderWorkspace(dom["workspace-body"], state, onToggleNode);
  updateRetryButton();
  startEventPolling();
}

function onToggleNode(key) {
  toggleNode(state, key);
  renderWorkspace(dom["workspace-body"], state, onToggleNode);
  updateRetryButton();
}

function updateRetryButton() {
  const trace = state.selectedTrace;
  dom["retry-button"].hidden = !(state.authenticated && trace && isTerminal(trace.status));
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
      renderWorkspace(dom["workspace-body"], state, onToggleNode);
    }
    state.polling.failures = 0;
    const terminal = state.selectedTrace && isTerminal(state.selectedTrace.status);
    if (terminal) {
      // Refresh the detail once so conversation input/output and final status land.
      await reloadSelectedDetail();
    }
    schedulePoll(terminal ? 5000 : 1000);
  } catch {
    state.polling.stale = true;
    const delay = BACKOFF[Math.min(state.polling.failures, BACKOFF.length - 1)];
    state.polling.failures += 1;
    schedulePoll(delay);
  }
}

let lastReloadedStatus = null;
async function reloadSelectedDetail() {
  const traceId = state.selectedTraceId;
  if (!traceId) return;
  const key = `${traceId}:${state.selectedTrace && state.selectedTrace.status}`;
  if (key === lastReloadedStatus) return;
  lastReloadedStatus = key;
  const { ok, body } = await requestJson(`/api/v1/traces/${traceId}`);
  if (ok && body && traceId === state.selectedTraceId) {
    body.events = state.events.length ? state.events : body.events;
    state.selectedTrace = body;
    renderWorkspace(dom["workspace-body"], state, onToggleNode);
    updateRetryButton();
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
    showAlert(t("alert.retryRejected"));
    return;
  }
  state.selectedTraceId = null;
  await refreshTraces();
  await selectAndLoad(body.trace_id);
}

// --- language -------------------------------------------------------------

function onToggleLang() {
  toggleLang();
  applyStatic();
  renderTraceList(dom["trace-items"], state, selectAndLoad);
  renderWorkspace(dom["workspace-body"], state, onToggleNode);
  if (chatroom) chatroom.render();
}

// --- bootstrap ------------------------------------------------------------

function init() {
  cacheDom();
  applyTheme(storedTheme());
  applyStatic();
  chatroom = createChatroom({
    transcript: dom["chat-transcript"],
    onTrace: onChatTrace,
    onBusy: onChatBusy,
    onError: (message) => showAlert(message),
  });
  chatroom.render();
  tunePanel = createTunePanel({
    dialog: dom["tune-dialog"],
    body: dom["tune-body"],
    onError: (message) => showAlert(message),
  });
  renderWorkspace(dom["workspace-body"], state, onToggleNode);
  dom["token-form"].addEventListener("submit", onTokenSubmit);
  dom["logout-button"].addEventListener("click", logout);
  dom["lang-toggle"].addEventListener("click", onToggleLang);
  dom["theme-toggle"].addEventListener("click", onToggleTheme);
  dom["tune-button"].addEventListener("click", () => tunePanel.open());
  dom["tune-close"].addEventListener("click", () => tunePanel.close());
  dom["retry-button"].addEventListener("click", openRetry);
  dom["retry-form"].addEventListener("submit", onRetrySubmit);
  dom["chat-form"].addEventListener("submit", onChatSubmit);
  dom["chat-reset"].addEventListener("click", onChatReset);
  window.addEventListener("beforeunload", () => { stopEventPolling(); stopListRefresh(); });
  requireAuth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
