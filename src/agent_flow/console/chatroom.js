// Shared customer chatroom: submit a message, poll to a safe terminal reply,
// render role-tagged bubbles. Used by the standalone chat page and by the
// embedded chat panel on the demo console. Only safe customer-facing text is
// ever shown here — never drafts or model reasoning.
import { requestJson } from "./api.js";
import { t } from "./i18n.js";

function statusText(status) {
  const label = t(`status.${status}`);
  return label === `status.${status}` ? status : label;
}

function replyMessage(body) {
  if (body.status === "completed") {
    if (body.handoff && body.handoff.safe_message) {
      return { role: "agent", text: body.handoff.safe_message };
    }
    return { role: "agent", text: body.text || statusText("completed") };
  }
  return { role: "status", text: statusText(body.status) };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// The session id is not a secret — unlike the bearer token it survives a
// reload, so refreshing the page continues the same conversation instead of
// orphaning it server-side and starting cold.
const SESSION_KEY = "tracewright.session";

function storedSessionId() {
  try {
    return window.localStorage.getItem(SESSION_KEY) || null;
  } catch {
    return null;
  }
}

function rememberSessionId(value) {
  try {
    window.localStorage.setItem(SESSION_KEY, value);
  } catch {
    /* private mode: the session simply will not survive a reload */
  }
}

function forgetSessionId() {
  try {
    window.localStorage.removeItem(SESSION_KEY);
  } catch {
    /* nothing to forget */
  }
}

function timeLabel(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function createChatroom({ transcript, onTrace, onError, onBusy }) {
  const state = { sessionId: storedSessionId(), messages: [], sending: false };

  function setSending(value) {
    state.sending = value;
    if (onBusy) onBusy(value);
  }

  function pushMessage(role, text, extra = {}) {
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === role && last.text === text) return;
    if (role === "status" && last && last.role === "status") state.messages.pop();
    state.messages.push({ role, text, ...extra });
    render();
  }

  function render() {
    transcript.textContent = "";
    if (state.messages.length === 0) {
      const empty = document.createElement("p");
      empty.className = "chat-empty";
      empty.textContent = t("chat.empty");
      transcript.appendChild(empty);
      return;
    }
    for (const message of state.messages) {
      const row = document.createElement("div");
      row.className = `chat-row chat-${message.role}`;
      const bubble = document.createElement("div");
      bubble.className = "chat-bubble";
      bubble.textContent = message.text;
      row.appendChild(bubble);
      const stamp = timeLabel(message.createdAt);
      if (stamp) {
        const time = document.createElement("span");
        time.className = "chat-time";
        time.textContent = stamp;
        row.appendChild(time);
      }
      transcript.appendChild(row);
    }
    transcript.scrollTop = transcript.scrollHeight;
  }

  // Replay what this session already said, so a reload does not look like a
  // fresh conversation to the person even though the server remembers it.
  async function restore() {
    if (!state.sessionId) return;
    const { ok, body } = await requestJson(
      `/api/v1/sessions/${encodeURIComponent(state.sessionId)}/messages`
    );
    if (!ok || !body || !Array.isArray(body.messages)) return;
    state.messages = body.messages.map((message) => ({
      role: message.role,
      text: message.text,
      createdAt: message.created_at,
    }));
    render();
  }

  function reset() {
    forgetSessionId();
    state.sessionId = null;
    state.messages = [];
    render();
  }

  // Read another chat: the same transcript view, a different chat id. Sending
  // then continues that conversation, which is what an operator answering a
  // LINE thread expects.
  async function switchTo(sessionId) {
    if (!sessionId || state.sending) return;
    state.sessionId = sessionId;
    rememberSessionId(sessionId);
    state.messages = [];
    render();
    await restore();
  }

  async function submit(text) {
    if (!text || state.sending) return;
    if (!state.sessionId) {
      state.sessionId = `console-${crypto.randomUUID()}`;
      rememberSessionId(state.sessionId);
    }
    const messageId = crypto.randomUUID();
    setSending(true);
    try {
      pushMessage("customer", text, { createdAt: new Date().toISOString() });
      const { status, body } = await requestJson("/api/v1/submissions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          channel: "console",
          external_message_id: messageId,
          session_id: state.sessionId,
          text,
          idempotency_key: messageId,
          metadata: { source: "customer-chat" },
        }),
      });
      if (status !== 202 || !body) {
        if (onError) onError(t("alert.submitRejected"));
        return;
      }
      if (onTrace) onTrace(body.trace_id);
      await poll(body.submission_id);
    } finally {
      setSending(false);
    }
  }

  async function poll(submissionId) {
    for (let attempt = 0; attempt < 600; attempt += 1) {
      const { ok, body } = await requestJson(`/api/v1/submissions/${submissionId}`);
      if (!ok || !body) break;
      const message = replyMessage(body);
      pushMessage(message.role, message.text, {
        createdAt: message.role === "agent" ? new Date().toISOString() : undefined,
      });
      if (body.status === "completed" || body.status === "failed") break;
      await sleep(500);
    }
  }

  return {
    render,
    submit,
    restore,
    reset,
    switchTo,
    sending: () => state.sending,
    sessionId: () => state.sessionId,
  };
}
