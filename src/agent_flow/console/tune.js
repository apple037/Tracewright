// Admin panel: shows what the assistant is currently running with — the
// instructions for each step, the voice, and which model does what — and lets
// the instructions and voice be edited live.
//
// Saving does not rewrite the YAML under config/; it layers an override the
// server applies to the next turn. Every save changes the artifact checksum,
// and that checksum is stamped on the spans of the turns that used it, so a
// trace always identifies the exact text that produced it.
import { requestJson } from "./api.js";
import { el } from "./render.js";
import { t } from "./i18n.js";

function badge(edited) {
  return el("span", {
    class: `tune-badge${edited ? " is-edited" : ""}`,
    text: edited ? t("tune.edited") : t("tune.fromFile"),
  });
}

function section(titleKey, children) {
  const node = el("section", { class: "tune-section" });
  node.appendChild(el("h3", { text: t(titleKey) }));
  for (const child of children) node.appendChild(child);
  return node;
}

// One editable card. `save` and `revert` take the textarea value and return the
// server's fresh view of the artifact, which is what we re-render from.
function editableCard({ name, meta, edited, value, save, revert, onError }) {
  const card = el("div", { class: "tune-card" });
  const head = el("div", { class: "tune-card-head" });
  head.appendChild(el("span", { class: "tune-name", text: name }));
  head.appendChild(el("span", { class: "tune-meta", text: meta }));
  const marker = badge(edited);
  head.appendChild(marker);
  card.appendChild(head);

  const area = el("textarea", { "aria-label": name });
  area.value = value || "";
  card.appendChild(area);

  const actions = el("div", { class: "tune-actions" });
  const saveButton = el("button", { type: "button", text: t("tune.save") });
  const revertButton = el("button", {
    type: "button", class: "ghost-button", text: t("tune.revert"),
  });
  const note = el("span", { class: "tune-saved" });
  actions.appendChild(saveButton);
  actions.appendChild(revertButton);
  actions.appendChild(note);
  card.appendChild(actions);

  async function run(action, payload) {
    saveButton.disabled = true;
    revertButton.disabled = true;
    note.textContent = "";
    try {
      const result = await action(payload);
      if (!result) {
        onError(t("alert.saveFailed"));
        return;
      }
      area.value = result.system_prompt ?? result.style_prompt ?? area.value;
      marker.replaceWith(badge(Boolean(result.edited)));
      note.textContent = t("tune.saved");
    } finally {
      saveButton.disabled = false;
      revertButton.disabled = false;
    }
  }

  saveButton.addEventListener("click", () => run(save, area.value));
  revertButton.addEventListener("click", () => run(revert, null));
  return card;
}

async function put(path, body) {
  const { ok, body: result } = await requestJson(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return ok ? result : null;
}

async function del(path) {
  const { ok, body } = await requestJson(path, { method: "DELETE" });
  return ok ? body : null;
}

function modelsTable(models) {
  const wrap = el("div", { class: "tune-scroll" });
  const table = el("table", { class: "tune-table" });
  const head = el("tr");
  for (const key of ["tune.role", "tune.model", "tune.settings"]) {
    head.appendChild(el("th", { text: t(key) }));
  }
  table.appendChild(head);
  for (const [role, info] of Object.entries(models.roles || {})) {
    const row = el("tr", { class: info.disabled ? "is-off" : "" });
    row.appendChild(el("td", { text: role }));
    row.appendChild(el("td", { text: info.disabled ? t("tune.disabled") : info.model }));
    row.appendChild(el("td", {
      text: `${info.profile} · temp ${info.temperature} · ${info.structured_output}`,
    }));
    table.appendChild(row);
  }
  wrap.appendChild(table);
  wrap.appendChild(el("p", { class: "tune-meta", text: models.config_path || "" }));
  return wrap;
}

function settingsList(settings) {
  const list = el("div", { class: "tune-scroll" });
  const table = el("table", { class: "tune-table" });
  for (const [key, value] of Object.entries(settings || {})) {
    const row = el("tr");
    row.appendChild(el("th", { text: key }));
    row.appendChild(el("td", { text: String(value) }));
    table.appendChild(row);
  }
  list.appendChild(table);
  return list;
}

export function createTunePanel({ dialog, body, onError }) {
  async function open() {
    dialog.showModal();
    body.textContent = "";
    body.setAttribute("aria-busy", "true");
    const { ok, body: config } = await requestJson("/api/v1/config");
    body.removeAttribute("aria-busy");
    if (!ok || !config) {
      onError(t("alert.configFailed"));
      dialog.close();
      return;
    }
    render(config);
  }

  function render(config) {
    body.textContent = "";

    body.appendChild(section("tune.prompts", (config.prompts || []).map((prompt) =>
      editableCard({
        name: prompt.node,
        meta: `v${prompt.version} · ${String(prompt.checksum).slice(0, 12)}`,
        edited: prompt.edited,
        value: prompt.system_prompt,
        save: (text) => put(`/api/v1/config/prompts/${encodeURIComponent(prompt.node)}`, {
          system_prompt: text,
        }),
        revert: () => del(`/api/v1/config/prompts/${encodeURIComponent(prompt.node)}`),
        onError,
      })
    )));

    body.appendChild(section("tune.personas", (config.personas || []).map((persona) =>
      editableCard({
        name: persona.artifact_id,
        meta: `${t("tune.appliesTo")} ${(persona.applies_to || []).join(", ")}`,
        edited: persona.edited,
        value: persona.style_prompt,
        save: (text) => put(
          `/api/v1/config/personas/${encodeURIComponent(persona.artifact_id)}`,
          { style_prompt: text }
        ),
        revert: () => del(
          `/api/v1/config/personas/${encodeURIComponent(persona.artifact_id)}`
        ),
        onError,
      })
    )));

    body.appendChild(section("tune.models", [modelsTable(config.models || {})]));
    body.appendChild(section("tune.settings", [settingsList(config.settings || {})]));
  }

  return { open, close: () => dialog.close() };
}
