import { api } from "../lib/api.js";
import { copyText } from "../lib/clipboard.js";
import { fmt, h } from "../lib/dom.js";
import { icon } from "../components/icons.js";
import { menu, togglePopover } from "../components/popover.js";
import { toast } from "../components/toast.js";

/** Stage names the API takes, in interview order, with their labels. */
export const STAGE_LABELS = { screen: "Screen", oa: "Online assessment", onsite: "Onsite",
  final: "Final round", other: "Other" };
const STAGED = ["interviewing", "offer"];
const UNTRACKED = [null, undefined, "passed"];

const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`;

/** The id a stage event carries for PATCH and DELETE. */
const eventId = (event) => event.id ?? event.event_id;

/** A stored ISO time as a datetime-local value in this browser's time zone. */
function localInput(at) {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** The stage rows of a posting's events, soonest first. */
export function stagesOf(job) {
  return (job.events || []).filter((event) => event.stage)
    .sort((a, b) => String(a.at).localeCompare(String(b.at)));
}

// ---------------------------------------------------------------------------
// Stages
// ---------------------------------------------------------------------------
/** Which stage form is open: {jobId, event} for an edit, event null for a new one. */
let stageForm = null;

/**
 * The Stages section of the detail pane: the list with an edit and delete menu per
 * stage and the add form, shown from interviewing on; an applied posting gets an
 * "Add a stage" button instead. `onChange` runs after the API took a change and
 * resolves to the posting as it now is, or null when it could not be read.
 * @param {HTMLElement} box @param {object} job @param {{onChange: () => Promise<object | null>}} options
 */
export function renderStages(box, job, { onChange }) {
  const stages = stagesOf(job);
  const open = stageForm?.jobId === job.id;
  const full = STAGED.includes(job.status) || stages.length > 0;
  box.hidden = !full && job.status !== "applied" && !open;
  if (box.hidden) {
    box.replaceChildren();
    return;
  }
  const add = h("button", { type: "button", class: "btn btn-sm", "data-key": "stage-add",
    onclick: () => openStageForm(box, job, null, onChange) }, icon("plus"), full ? "Add stage" : "Add a stage");
  if (!full && !open) {
    box.replaceChildren(h("div", { class: "section-head" },
      h("h3", { class: "section-label", text: "Stages" }), add),
    h("p", { class: "muted stage-hint", text: "Add a screen or interview once it is booked. Today lists it, and the calendar download includes it." }));
    return;
  }
  const now = Date.now();
  box.replaceChildren(...[
    h("div", { class: "section-head" }, h("h3", { class: "section-label", text: "Stages" }), !open && add),
    stages.length ? h("ol", { class: "stages" }, stages.map((event) => {
      const past = Date.parse(event.at) < now;
      const more = h("button", { type: "button", class: "icon-btn", "aria-haspopup": "menu", "aria-expanded": "false",
        "aria-label": `Edit or delete the ${STAGE_LABELS[event.stage] || event.stage} stage`, title: "Edit or delete" },
      icon("more"));
      more.addEventListener("click", () => togglePopover(more, () => menu([
        { label: "Edit", run: () => openStageForm(box, job, event, onChange) },
        { label: "Delete", tone: "weak", run: () => deleteStage(box, job, event, onChange) },
      ]), { label: "Stage actions", align: "end" }));
      return h("li", { class: `stage${past ? " stage-past" : ""}` },
        h("span", { class: "stage-name", text: STAGE_LABELS[event.stage] || event.stage }),
        h("time", { class: "stage-at mono", datetime: event.at, text: fmt.dateTime(event.at), title: fmt.full(event.at) }),
        more,
        event.note && h("span", { class: "stage-note", text: event.note }));
    })) : !open && h("p", { class: "muted", text: "No stages yet." }),
    open && stageFormEl(box, job, stageForm.event, onChange)].filter(Boolean));
}

function openStageForm(box, job, event, onChange) {
  stageForm = { jobId: job.id, event };
  renderStages(box, job, { onChange });
  box.querySelector(".stage-form select")?.focus();
}

/** Close the stage form and put focus on Add stage, once the list shows the change. */
function closeStageForm(box, job, onChange) {
  stageForm = null;
  renderStages(box, job, { onChange });
  box.querySelector('[data-key="stage-add"]')?.focus();
}

/**
 * Open the add-stage form for the posting on show, for the palette.
 * @param {HTMLElement} box @param {object} job @param {() => Promise<object | null>} onChange
 */
export function addStage(box, job, onChange) {
  openStageForm(box, job, null, onChange);
  box.scrollIntoView({ block: "nearest" });
}

/** The fields of an edit that differ from the stage, for PATCH. */
function stageChanges(event, body) {
  const changes = {};
  if (body.stage !== event.stage) changes.stage = body.stage;
  if (body.at.slice(0, 16) !== localInput(event.at)) changes.at = body.at;
  if (body.note !== (event.note || "")) changes.note = body.note;
  return changes;
}

function stageFormEl(box, job, event, onChange) {
  const close = () => closeStageForm(box, job, onChange);
  const stage = h("select", { class: "select", "aria-label": "Stage" },
    Object.entries(STAGE_LABELS).map(([value, label]) => h("option", { value, text: label,
      selected: value === (event?.stage ?? "screen") })));
  const at = h("input", { type: "datetime-local", class: "input", "aria-label": "Date and time",
    value: event ? localInput(event.at) : "", required: true });
  const note = h("input", { type: "text", class: "input", placeholder: "Note (interviewer, link, prep…)",
    "aria-label": "Stage note", value: event?.note || "" });
  const error = h("p", { class: "field-error", role: "alert", hidden: true });
  const save = h("button", { type: "submit", class: "btn btn-primary btn-sm", text: event ? "Save stage" : "Add stage" });
  return h("form", { class: "stage-form", onkeydown: (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    close();
  }, onsubmit: async (e) => {
    e.preventDefault();
    if (!at.value) {
      error.hidden = false;
      error.textContent = "Pick a date and time.";
      at.focus();
      return;
    }
    // naive local ISO, as the store writes its own timestamps
    const body = { stage: stage.value, at: `${at.value}:00`, note: note.value.trim() };
    const changes = event ? stageChanges(event, body) : body;
    if (!Object.keys(changes).length) {
      close();
      return;
    }
    save.disabled = true;
    try {
      if (event) await api(`/api/stages/${encodeURIComponent(eventId(event))}`, { method: "PATCH", body: changes, quiet: true });
      else await api(`${jobPath(job.id)}/stages`, { method: "POST", body, quiet: true });
      toast(event ? `Updated the ${STAGE_LABELS[body.stage]} stage` : `Added a ${STAGE_LABELS[body.stage]} stage`);
      stageForm = null;
      const fresh = await onChange();
      closeStageForm(box, fresh || job, onChange);
    } catch (err) {
      error.hidden = false;
      error.textContent = `Couldn't save the stage: ${err.message}`;
      save.disabled = false;
    }
  } },
  h("div", { class: "stage-form-row" }, stage, at),
  note, error,
  h("div", { class: "pop-actions" },
    h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Cancel", onclick: close }), save));
}

async function deleteStage(box, job, event, onChange) {
  const label = STAGE_LABELS[event.stage] || event.stage;
  try {
    await api(`/api/stages/${encodeURIComponent(eventId(event))}`, { method: "DELETE" });
  } catch {
    // api() has shown the error
    return;
  }
  toast(`Deleted the ${label} stage`, { undo: async () => {
    try {
      await api(`${jobPath(job.id)}/stages`, { method: "POST", body: { stage: event.stage, at: event.at, note: event.note || "" } });
      await onChange();
    } catch {
      // api() has shown the error
    }
  } });
  await onChange();
  box.querySelector('[data-key="stage-add"]')?.focus();
}

// ---------------------------------------------------------------------------
// Checklist and files
// ---------------------------------------------------------------------------
/** @returns {boolean} whether a posting's status earns it a checklist and files */
export function tracksPrep(job) {
  return !UNTRACKED.includes(job.status);
}

/**
 * The Checklist and Files sections for a tracked posting, loaded from the API; an
 * untracked posting hides the box. Call again to reload, as on `tracking_changed`.
 * @param {HTMLElement} box @param {object} job
 */
export async function renderPrep(box, job) {
  box.hidden = !tracksPrep(job);
  if (box.hidden) {
    box.replaceChildren();
    return;
  }
  // an inline edit in progress keeps its field until it is saved or cancelled
  if (box.querySelector(".check-edit")) return;
  if (!box.childElementCount) {
    box.replaceChildren(h("section", { class: "detail-section" }, h("h3", { class: "section-label", text: "Checklist" }),
      h("p", { class: "muted", text: "Loading…" })));
  }
  const [items, files] = await Promise.allSettled([api(`${jobPath(job.id)}/checklist`, { quiet: true }),
    api(`${jobPath(job.id)}/attachments`, { quiet: true })]);
  if (!box.isConnected || box.dataset.job !== job.id) return;
  const focused = box.contains(document.activeElement)
    ? /** @type {HTMLElement} */ (document.activeElement).dataset.key : null;
  const reload = () => renderPrep(box, job);
  box.replaceChildren(
    checklistSection(job, items, reload),
    filesSection(job, files, reload));
  const again = focused && box.querySelector(`[data-key="${CSS.escape(focused)}"]`);
  // a moved item's arrow is disabled at the end of the list, so its label takes focus
  (again?.disabled ? again.closest(".check-item")?.querySelector(".check-label") : again)?.focus();
}

function loadError(noun, result, reload) {
  return h("div", { class: "prep-error" },
    h("p", { class: "field-error", text: `Couldn't load the ${noun}: ${result.reason.message}` }),
    h("button", { type: "button", class: "btn btn-sm", text: "Try again", onclick: reload }));
}

function checklistSection(job, result, reload) {
  const head = h("div", { class: "section-head" }, h("h3", { class: "section-label", text: "Checklist" }));
  if (result.status === "rejected") return h("section", { class: "detail-section" }, head, loadError("checklist", result, reload));
  const items = [...result.value].sort((a, b) => a.position - b.position);
  const done = items.filter((item) => item.done).length;
  if (items.length) head.append(h("span", { class: "check-progress mono", text: `${done}/${items.length}` }));
  const path = (item) => `/api/checklist/${encodeURIComponent(item.id)}`;
  const change = async (request) => {
    try {
      await request();
    } catch {
      // api() has shown the error; the reload shows what the server holds
    }
    reload();
  };
  const reorder = (from, to) => {
    if (to < 0 || to >= items.length || from === to) return;
    const ids = items.map((item) => item.id);
    const [moved] = ids.splice(from, 1);
    ids.splice(to, 0, moved);
    change(() => api(`${jobPath(job.id)}/checklist/order`, { method: "PUT", body: { ids } }));
  };
  let dragged = null;
  const list = h("ul", { class: "checklist" }, items.map((item, index) => {
    const label = h("button", { type: "button", class: "check-label", text: item.label, "data-key": `label-${item.id}`,
      title: "Edit the item", onclick: () => editLabel(label, item, (text) => change(() => api(path(item),
        { method: "PATCH", body: { label: text } })), reload) });
    const li = h("li", { class: `check-item${item.done ? " is-done" : ""}`, draggable: "true", "data-index": String(index) },
      h("span", { class: "check-grip", "aria-hidden": "true" }, icon("grip")),
      h("input", { type: "checkbox", checked: Boolean(item.done), "data-key": `done-${item.id}`,
        "aria-label": item.label, onchange: (e) => change(() => api(path(item),
          { method: "PATCH", body: { done: e.target.checked } })) }),
      label,
      h("span", { class: "check-tools" },
        h("button", { type: "button", class: "icon-btn", "data-key": `up-${item.id}`, disabled: index === 0,
          "aria-label": `Move ${item.label} up`, title: "Move up", onclick: () => reorder(index, index - 1) }, icon("arrowUp")),
        h("button", { type: "button", class: "icon-btn", "data-key": `down-${item.id}`, disabled: index === items.length - 1,
          "aria-label": `Move ${item.label} down`, title: "Move down", onclick: () => reorder(index, index + 1) }, icon("arrowDown")),
        h("button", { type: "button", class: "icon-btn", "aria-label": `Delete ${item.label}`, title: "Delete",
          onclick: () => change(() => api(path(item), { method: "DELETE" })) }, icon("trash"))));
    li.addEventListener("dragstart", (event) => {
      dragged = index;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(item.id));
      li.classList.add("dragging");
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", (event) => {
      if (dragged == null) return;
      event.preventDefault();
      li.classList.add("drop-before");
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-before"));
    li.addEventListener("drop", (event) => {
      event.preventDefault();
      li.classList.remove("drop-before");
      if (dragged != null) reorder(dragged, index);
      dragged = null;
    });
    return li;
  }));
  const entry = h("input", { type: "text", class: "input", placeholder: "Add an item", "aria-label": "New checklist item",
    "data-key": "check-new" });
  const form = h("form", { class: "add-row check-add", onsubmit: (event) => {
    event.preventDefault();
    const text = entry.value.trim();
    if (!text) return;
    change(() => api(`${jobPath(job.id)}/checklist`, { method: "POST", body: { label: text } }));
  } }, entry, h("button", { type: "submit", class: "btn btn-sm" }, icon("plus"), "Add"));
  return h("section", { class: "detail-section" }, head,
    items.length ? list : h("p", { class: "muted", text: "Nothing to do yet. Add what this application needs." }), form);
}

function editLabel(label, item, save, cancel) {
  const input = h("input", { type: "text", class: "input check-edit", value: item.label, "aria-label": "Item label" });
  let settled = false;
  const finish = (keep) => {
    if (settled) return;
    settled = true;
    const text = input.value.trim();
    input.remove();
    if (keep && text && text !== item.label) save(text);
    else cancel();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
  label.replaceWith(input);
  input.focus();
  input.select();
}

function filesSection(job, result, reload) {
  const head = h("div", { class: "section-head" }, h("h3", { class: "section-label", text: "Files" }));
  if (result.status === "rejected") return h("section", { class: "detail-section" }, head, loadError("files", result, reload));
  const files = result.value;
  const labelInput = h("input", { type: "text", class: "input", placeholder: "Label, e.g. Resume v3",
    "aria-label": "File label", "data-key": "file-label" });
  const pathInput = h("input", { type: "text", class: "input mono", placeholder: "/Users/you/Documents/resume.pdf",
    "aria-label": "File path", spellcheck: "false", "data-key": "file-path" });
  const error = h("p", { class: "field-error", role: "alert", hidden: true });
  const form = h("form", { class: "file-add", onsubmit: async (event) => {
    event.preventDefault();
    const path = pathInput.value.trim();
    if (!path) {
      pathInput.focus();
      return;
    }
    try {
      await api(`${jobPath(job.id)}/attachments`, { method: "POST", quiet: true,
        body: { label: labelInput.value.trim() || path.split("/").pop(), path } });
      reload();
    } catch (err) {
      error.hidden = false;
      error.textContent = `Couldn't add the file: ${err.message}`;
    }
  } }, labelInput, pathInput, h("button", { type: "submit", class: "btn btn-sm" }, icon("plus"), "Add file"));
  return h("section", { class: "detail-section" }, head,
    files.length ? h("ul", { class: "files" }, files.map((file) => h("li", { class: "file" },
      icon("file"),
      h("span", { class: "file-text" }, h("span", { class: "file-label", text: file.label }),
        h("span", { class: "file-path mono", text: file.path, title: file.path })),
      h("button", { type: "button", class: "icon-btn", "aria-label": `Copy the path of ${file.label}`, title: "Copy the path",
        onclick: () => copyText(file.path, "the path") }, icon("copy")),
      h("button", { type: "button", class: "icon-btn", "aria-label": `Remove ${file.label}`, title: "Remove",
        onclick: async () => {
          try {
            await api(`/api/attachments/${encodeURIComponent(file.id)}`, { method: "DELETE" });
          } catch {
            // api() has shown the error
          }
          reload();
        } }, icon("trash")))))
      : h("p", { class: "muted", text: "No files yet. Add the resume and cover letter you sent for this job." }),
    form, error,
    h("p", { class: "field-help", text: "Cairn links to the file where it sits on your Mac." }));
}
