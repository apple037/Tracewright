import { setToken, clearToken } from "./api.js";
import { applyStatic, toggleLang } from "./i18n.js";
import { createChatroom } from "./chatroom.js";
import { showAlert } from "./render.js";

const THEME_KEY = "tracewright.theme";
const dom = {};
let chatroom = null;

function cacheDom() {
  for (const id of [
    "token-dialog", "token-form", "token-input", "logout-button", "lang-toggle",
    "theme-toggle", "chat-transcript", "chat-form", "chat-input", "chat-send",
    "chat-reset",
  ]) {
    dom[id] = document.getElementById(id);
  }
}

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

function requireAuth() {
  dom["token-dialog"].showModal();
}

async function onTokenSubmit(event) {
  event.preventDefault();
  const value = dom["token-input"].value;
  if (!value.trim()) return;
  setToken(value);
  dom["token-input"].value = "";
  dom["token-dialog"].close();
  dom["logout-button"].hidden = false;
  await chatroom.restore();
  dom["chat-input"].focus();
}

function logout() {
  clearToken();
  requireAuth();
}

// Disabling send while a turn is in flight is what stops a second message being
// silently swallowed after the textarea has already been cleared.
function onBusy(busy) {
  dom["chat-send"].disabled = busy;
  dom["chat-input"].readOnly = busy;
  dom["chat-transcript"].setAttribute("aria-busy", busy ? "true" : "false");
}

async function onSubmit(event) {
  event.preventDefault();
  const text = dom["chat-input"].value.trim();
  if (!text || !chatroom || chatroom.sending()) return;
  dom["chat-input"].value = "";
  await chatroom.submit(text);
}

function onReset() {
  if (!chatroom || chatroom.sending()) return;
  chatroom.reset();
  dom["chat-input"].focus();
}

function onToggleLang() {
  toggleLang();
  applyStatic();
  chatroom.render();
}

function init() {
  cacheDom();
  applyTheme(storedTheme());
  applyStatic();
  chatroom = createChatroom({
    transcript: dom["chat-transcript"],
    onBusy,
    onError: (message) => showAlert(message),
  });
  chatroom.render();
  dom["token-form"].addEventListener("submit", onTokenSubmit);
  dom["logout-button"].addEventListener("click", logout);
  dom["lang-toggle"].addEventListener("click", onToggleLang);
  dom["theme-toggle"].addEventListener("click", onToggleTheme);
  dom["chat-form"].addEventListener("submit", onSubmit);
  dom["chat-reset"].addEventListener("click", onReset);
  requireAuth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
