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
  "io.reasoning": { "zh-TW": "模型思考過程（僅管理員）", "en-US": "Model reasoning (admin only)" },
  "io.pass": { "zh-TW": "第", "en-US": "pass" },
  "io.attempt": { "zh-TW": "嘗試", "en-US": "attempt" },
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
  "detail.empty": { "zh-TW": "無其他可顯示的資訊", "en-US": "Additional safe metadata unavailable" },
  "alert.traceFailed": { "zh-TW": "無法載入這筆追蹤的細節。", "en-US": "Unable to load this trace." },
  "alert.configFailed": { "zh-TW": "無法載入設定（需要管理員權杖）。", "en-US": "Unable to load settings — an admin token is required." },
  "alert.saveFailed": { "zh-TW": "儲存失敗。", "en-US": "Save failed." },

  "header.theme": { "zh-TW": "切換主題", "en-US": "Toggle theme" },
  "header.tune": { "zh-TW": "調整設定", "en-US": "Tune" },
  "chat.new": { "zh-TW": "新對話", "en-US": "New conversation" },
  "chat.thinking": { "zh-TW": "思考中…", "en-US": "Thinking…" },
  "list.title": { "zh-TW": "本次對話的回合", "en-US": "Turns in this conversation" },
  "flow.title": { "zh-TW": "這一回合發生了什麼", "en-US": "What happened this turn" },
  "flow.empty": { "zh-TW": "傳送訊息後，這裡會即時顯示每個步驟。", "en-US": "Send a message and each step appears here as it runs." },
  "flow.failedAt": { "zh-TW": "失敗於", "en-US": "Failed at" },

  "tune.title": { "zh-TW": "調整助理", "en-US": "Tune the assistant" },
  "tune.close": { "zh-TW": "關閉", "en-US": "Close" },
  "tune.prompts": { "zh-TW": "每個步驟的指示", "en-US": "Instructions per step" },
  "tune.personas": { "zh-TW": "語氣（人設）", "en-US": "Voice (persona)" },
  "tune.models": { "zh-TW": "模型", "en-US": "Models" },
  "tune.settings": { "zh-TW": "執行設定", "en-US": "Runtime settings" },
  "tune.save": { "zh-TW": "儲存", "en-US": "Save" },
  "tune.revert": { "zh-TW": "還原成檔案內容", "en-US": "Revert to file" },
  "tune.edited": { "zh-TW": "已在此編輯", "en-US": "Edited here" },
  "tune.fromFile": { "zh-TW": "來自設定檔", "en-US": "From file" },
  "tune.saved": { "zh-TW": "已儲存。下一則訊息就會套用。", "en-US": "Saved. It applies to the next message." },
  "tune.appliesTo": { "zh-TW": "套用於", "en-US": "Applies to" },
  "tune.role": { "zh-TW": "步驟", "en-US": "Step" },
  "tune.model": { "zh-TW": "模型", "en-US": "Model" },
  "tune.disabled": { "zh-TW": "已停用", "en-US": "Off" },
  "tune.checksum": { "zh-TW": "版本指紋", "en-US": "Version fingerprint" },
  "tune.hint": {
    "zh-TW": "在這裡的修改會立即生效，但不會寫回設定檔；要永久保留請編輯 config/ 底下的 YAML。",
    "en-US": "Changes here take effect immediately but are not written back to the config files. To make one permanent, edit the YAML under config/.",
  },
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
