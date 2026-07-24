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

export function createChatroom({ transcript, input, onTrace, onError }) {
  const state = { sessionId: null, messages: [], sending: false };

  function pushMessage(role, text) {
    const last = state.messages[state.messages.length - 1];
    if (last && last.role === role && last.text === text) return;
    if (role === "status" && last && last.role === "status") state.messages.pop();
    state.messages.push({ role, text });
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
      transcript.appendChild(row);
    }
    transcript.scrollTop = transcript.scrollHeight;
  }

  async function submit(text) {
    if (!text || state.sending) return;
    if (!state.sessionId) state.sessionId = `console-${crypto.randomUUID()}`;
    const messageId = crypto.randomUUID();
    pushMessage("customer", text);
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
  }

  async function poll(submissionId) {
    state.sending = true;
    try {
      for (let attempt = 0; attempt < 600; attempt += 1) {
        const { ok, body } = await requestJson(`/api/v1/submissions/${submissionId}`);
        if (!ok || !body) break;
        const message = replyMessage(body);
        pushMessage(message.role, message.text);
        if (body.status === "completed" || body.status === "failed") break;
        await sleep(500);
      }
    } finally {
      state.sending = false;
    }
  }

  return { render, submit, sessionId: () => state.sessionId };
}
