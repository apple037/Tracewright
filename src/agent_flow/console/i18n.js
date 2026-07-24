// Minimal i18n for zh-TW / en-US. No framework, no storage: language lives in
// module memory and defaults to en-US. Call applyStatic() after a language
// change to retranslate the DOM.

const STRINGS = {
  "app.title": { "zh-TW": "Agent Flow · 追蹤主控台", "en-US": "Agent Flow · Trace Console" },
  "chat.title": { "zh-TW": "Agent Flow · 客服對話", "en-US": "Agent Flow · Customer Chat" },
  "lang.toggle": { "zh-TW": "EN", "en-US": "中文" },
  "header.openChat": { "zh-TW": "客服對話", "en-US": "Customer chat" },
  "header.openTrace": { "zh-TW": "追蹤主控台", "en-US": "Trace console" },
  "header.retry": { "zh-TW": "重試追蹤", "en-US": "Retry trace" },
  "header.signout": { "zh-TW": "登出", "en-US": "Sign out" },
  "token.title": { "zh-TW": "示範驗證", "en-US": "Demo authentication" },
  "token.label": { "zh-TW": "示範權杖", "en-US": "Demo token" },
  "token.enter": { "zh-TW": "進入", "en-US": "Enter" },
  "list.empty": { "zh-TW": "尚無追蹤紀錄。", "en-US": "No traces yet." },
  "workspace.select": { "zh-TW": "選擇一筆追蹤以檢視流程。", "en-US": "Select a trace to inspect its flow." },
  "workspace.io": { "zh-TW": "輸入與輸出", "en-US": "Input & output" },
  "io.input": { "zh-TW": "客戶訊息", "en-US": "Customer message" },
  "io.output": { "zh-TW": "回覆", "en-US": "Reply" },
  "io.citations": { "zh-TW": "引用", "en-US": "Citations" },
  "io.handoff": { "zh-TW": "轉真人", "en-US": "Handoff" },
  "io.noReply": { "zh-TW": "尚無回覆。", "en-US": "No reply yet." },
  "reasoning.summary": { "zh-TW": "推理摘要", "en-US": "Reasoning summary" },
  "reasoning.steps": { "zh-TW": "步驟", "en-US": "steps" },
  "retry.title": { "zh-TW": "重試追蹤", "en-US": "Retry trace" },
  "retry.reason": { "zh-TW": "重試原因", "en-US": "Retry reason" },
  "retry.cancel": { "zh-TW": "取消", "en-US": "Cancel" },
  "retry.confirm": { "zh-TW": "確認重試", "en-US": "Confirm retry" },
  "chat.header": { "zh-TW": "客服對話", "en-US": "Customer chat" },
  "chat.sub": { "zh-TW": "示範通道 · console", "en-US": "Demo channel · console" },
  "chat.placeholder": { "zh-TW": "輸入客戶訊息…", "en-US": "Type a customer message…" },
  "chat.send": { "zh-TW": "送出", "en-US": "Send message" },
  "chat.empty": { "zh-TW": "傳送訊息以開始對話。", "en-US": "Send a message to start the conversation." },
  "status.queued": { "zh-TW": "佇列中", "en-US": "Queued" },
  "status.running": { "zh-TW": "處理中", "en-US": "Running" },
  "status.completed": { "zh-TW": "已完成", "en-US": "Completed" },
  "status.succeeded": { "zh-TW": "成功", "en-US": "Succeeded" },
  "status.failed": { "zh-TW": "失敗", "en-US": "Failed" },
  "alert.tracesFailed": { "zh-TW": "無法載入追蹤紀錄。", "en-US": "Unable to load traces." },
  "alert.submitRejected": { "zh-TW": "訊息遭拒。", "en-US": "Submission was rejected." },
  "alert.retryRejected": { "zh-TW": "重試未被接受。", "en-US": "Retry was not accepted." },
};

let lang = "en-US";

export function getLang() {
  return lang;
}

export function toggleLang() {
  lang = lang === "zh-TW" ? "en-US" : "zh-TW";
  return lang;
}

export function t(key, params = {}) {
  const entry = STRINGS[key];
  let text = entry ? entry[lang] : key;
  for (const [name, value] of Object.entries(params)) {
    text = text.replace(`{${name}}`, String(value));
  }
  return text;
}

export function statusLabel(status) {
  return t(`status.${status}`) !== `status.${status}` ? t(`status.${status}`) : status;
}

// Retranslate every element tagged with data-i18n / data-i18n-placeholder.
export function applyStatic(root = document) {
  for (const node of root.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.getAttribute("data-i18n"));
  }
  for (const node of root.querySelectorAll("[data-i18n-placeholder]")) {
    node.setAttribute("placeholder", t(node.getAttribute("data-i18n-placeholder")));
  }
  const titleKey = document.body.getAttribute("data-title-key");
  if (titleKey) document.title = t(titleKey);
  document.documentElement.lang = lang;
}
