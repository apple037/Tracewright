import { setToken, clearToken } from "./api.js";
import { applyStatic, toggleLang } from "./i18n.js";
import { createChatroom } from "./chatroom.js";

const dom = {};
let chatroom = null;

function cacheDom() {
  for (const id of [
    "token-dialog", "token-form", "token-input", "logout-button", "lang-toggle",
    "chat-transcript", "chat-form", "chat-input",
  ]) {
    dom[id] = document.getElementById(id);
  }
}

function showAlert(message) {
  const region = document.getElementById("alert-region");
  region.textContent = "";
  if (message) {
    const p = document.createElement("p");
    p.textContent = message;
    region.appendChild(p);
  }
}

function requireAuth() {
  dom["token-dialog"].showModal();
}

function onTokenSubmit(event) {
  event.preventDefault();
  const value = dom["token-input"].value;
  if (!value.trim()) return;
  setToken(value);
  dom["token-input"].value = "";
  dom["token-dialog"].close();
  dom["logout-button"].hidden = false;
  dom["chat-input"].focus();
}

function logout() {
  clearToken();
  requireAuth();
}

async function onSubmit(event) {
  event.preventDefault();
  const text = dom["chat-input"].value.trim();
  if (!text || !chatroom) return;
  dom["chat-input"].value = "";
  await chatroom.submit(text);
}

function onToggleLang() {
  toggleLang();
  applyStatic();
  chatroom.render();
}

function init() {
  cacheDom();
  applyStatic();
  chatroom = createChatroom({
    transcript: dom["chat-transcript"],
    onError: (message) => showAlert(message),
  });
  chatroom.render();
  dom["token-form"].addEventListener("submit", onTokenSubmit);
  dom["logout-button"].addEventListener("click", logout);
  dom["lang-toggle"].addEventListener("click", onToggleLang);
  dom["chat-form"].addEventListener("submit", onSubmit);
  requireAuth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
