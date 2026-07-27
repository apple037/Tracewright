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
function editableCard({
  name, meta, edited, value, save, revert, onError,
  revertLabel = "tune.revert", showBadge = true, afterRevert = null,
}) {
  const card = el("div", { class: "tune-card" });
  const head = el("div", { class: "tune-card-head" });
  head.appendChild(el("span", { class: "tune-name", text: name }));
  head.appendChild(el("span", { class: "tune-meta", text: meta }));
  const marker = showBadge ? badge(edited) : el("span");
  head.appendChild(marker);
  card.appendChild(head);

  const area = el("textarea", { "aria-label": name });
  area.value = value || "";
  card.appendChild(area);

  const actions = el("div", { class: "tune-actions" });
  const saveButton = el("button", { type: "button", text: t("tune.save") });
  const revertButton = el("button", {
    type: "button", class: "ghost-button", text: t(revertLabel),
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
  revertButton.addEventListener("click", async () => {
    await run(revert, null);
    // Deleting a document removes the card entirely; reverting a prompt does not.
    if (afterRevert) afterRevert();
  });
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

function documentPath(source, sourceId) {
  return `/api/v1/config/knowledge/${encodeURIComponent(source)}/${
    encodeURIComponent(sourceId)}`;
}

// A form to add a document that does not exist yet. Editing an existing one is
// the same PUT, so only the id field is extra.
function addDocumentForm(source, onError, reload) {
  const card = el("div", { class: "tune-card" });
  card.appendChild(el("div", { class: "tune-card-head" }, [
    el("span", { class: "tune-name", text: t("tune.addDocument") }),
  ]));
  const id = el("input", { type: "text", "aria-label": t("tune.documentId") });
  id.placeholder = t("tune.documentId");
  const area = el("textarea", { "aria-label": t("tune.addDocument") });
  card.appendChild(id);
  card.appendChild(area);

  const actions = el("div", { class: "tune-actions" });
  const button = el("button", { type: "button", text: t("tune.add") });
  actions.appendChild(button);
  card.appendChild(actions);

  button.addEventListener("click", async () => {
    if (!id.value.trim() || !area.value.trim()) return;
    button.disabled = true;
    try {
      const saved = await put(documentPath(source, id.value.trim()),
        { content: area.value });
      if (!saved) {
        onError(t("alert.saveFailed"));
        return;
      }
      await reload();
    } finally {
      button.disabled = false;
    }
  });
  return card;
}

function knowledgeSection(sources, onError, reload) {
  const children = [el("p", { class: "tune-hint", text: t("tune.knowledgeHint") })];
  if (!sources.length) {
    children.push(el("p", { class: "tune-meta", text: t("tune.noKnowledge") }));
    return children;
  }
  for (const source of sources) {
    const labels = [source.type];
    if (!source.editable) labels.push(t("tune.readOnly"));
    if (!source.enabled) labels.push(t("tune.off"));
    children.push(el("div", { class: "tune-card-head" }, [
      el("span", { class: "tune-name", text: source.source }),
      el("span", { class: "tune-meta", text: labels.join(" · ") }),
    ]));

    const documents = source.documents || [];
    if (!documents.length) {
      children.push(el("p", { class: "tune-meta", text: t("tune.noDocuments") }));
    }
    for (const doc of documents) {
      children.push(editableCard({
        name: doc.source_id,
        meta: doc.version,
        value: doc.content,
        showBadge: false,
        revertLabel: "tune.delete",
        save: (text) => put(documentPath(source.source, doc.source_id),
          { content: text, version: doc.version }),
        revert: () => del(documentPath(source.source, doc.source_id)),
        afterRevert: reload,
        onError,
      }));
    }
    if (source.editable && source.enabled) {
      children.push(addDocumentForm(source.source, onError, reload));
    }
  }
  return children;
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

// What the assistant will actually be given of this chat on the next message —
// the windowed slice, not the whole transcript. The gap between the two counts
// is the answer to "why did it forget that".
function memorySection(memory, onReset, onRebuild) {
  const children = [];
  const { stored = 0, in_window: inWindow = 0 } = memory.exchanges || {};
  children.push(
    el("p", {
      class: "tune-hint",
      text: t("tune.memoryCounts")
        .replace("{stored}", stored)
        .replace("{window}", inWindow)
        .replace("{turns}", memory.history_turns),
    })
  );

  const scroll = el("div", { class: "tune-scroll" });
  if (!(memory.messages || []).length) {
    scroll.appendChild(el("p", { class: "tune-hint", text: t("tune.memoryEmpty") }));
  } else {
    const table = el("table", { class: "tune-table" });
    for (const message of memory.messages) {
      const row = el("tr");
      row.appendChild(el("th", { text: t(`tune.role.${message.role}`) }));
      row.appendChild(el("td", { text: message.text }));
      table.appendChild(row);
    }
    scroll.appendChild(table);
  }
  children.push(scroll);

  const actions = el("div", { class: "tune-actions" });
  const reset = el("button", {
    class: "ghost-button is-danger",
    type: "button",
    text: t("tune.memoryReset"),
  });
  // A soft delete, but it still takes the visible transcript with it, so it is
  // worth one question first.
  reset.addEventListener("click", () => {
    if (window.confirm(t("tune.memoryResetConfirm"))) onReset();
  });
  actions.appendChild(reset);

  const rebuild = el("button", {
    class: "ghost-button", type: "button", text: t("tune.memoryRebuild"),
  });
  rebuild.addEventListener("click", onRebuild);
  actions.appendChild(rebuild);
  children.push(actions);
  return children;
}

export function createTunePanel({
  dialog, body, onError, sessionId = () => null, onMemoryReset = () => {},
}) {
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
    await Promise.all([refreshKnowledge(), refreshMemory()]);
  }

  const memoryBody = el("div");

  async function refreshMemory() {
    memoryBody.textContent = "";
    const session = sessionId();
    if (!session) {
      memoryBody.appendChild(
        el("p", { class: "tune-hint", text: t("tune.memoryNoChat") })
      );
      return;
    }
    const path = `/api/v1/sessions/${encodeURIComponent(session)}/memory`;
    const { ok, body: memory } = await requestJson(path);
    if (!ok || !memory) {
      memoryBody.appendChild(
        el("p", { class: "tune-hint", text: t("tune.memoryUnavailable") })
      );
      return;
    }
    const children = memorySection(
      memory, () => resetMemory(path), () => rebuildMemory(path)
    );
    for (const child of children) memoryBody.appendChild(child);
  }

  async function resetMemory(path) {
    const { ok } = await requestJson(path, { method: "DELETE" });
    if (!ok) {
      onError(t("tune.memoryResetFailed"));
      return;
    }
    await refreshMemory();
    // The transcript reads the same rows, so the chat on screen is now stale.
    await onMemoryReset();
  }

  async function rebuildMemory(path) {
    const { ok } = await requestJson(`${path}/rebuild`, { method: "POST" });
    if (!ok) {
      onError(t("tune.memoryRebuildFailed"));
      return;
    }
    await refreshMemory();
    await onMemoryReset();
  }

  // Its own container so adding or deleting a document can re-render just this
  // section, without reopening the dialog and losing unsaved prompt edits.
  const knowledgeBody = el("div");

  async function refreshKnowledge() {
    const { ok, body: payload } = await requestJson("/api/v1/config/knowledge");
    knowledgeBody.textContent = "";
    // A runtime without an editable corpus answers 503; say so rather than
    // showing an empty box.
    const sources = ok && payload ? payload.sources || [] : [];
    for (const child of knowledgeSection(sources, onError, refreshKnowledge)) {
      knowledgeBody.appendChild(child);
    }
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

    body.appendChild(section("tune.knowledge", [knowledgeBody]));
    body.appendChild(section("tune.memory", [memoryBody]));
    body.appendChild(section("tune.models", [modelsTable(config.models || {})]));
    body.appendChild(section("tune.settings", [settingsList(config.settings || {})]));
  }

  return { open, close: () => dialog.close() };
}
