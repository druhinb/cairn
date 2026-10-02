import { api } from "../lib/api.js";
import { subscribe } from "../lib/events.js";
import { copyText } from "../lib/clipboard.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { MAC, register } from "../lib/keys.js";
import { readNumber } from "../lib/numbers.js";
import { setPref } from "../lib/store.js";
import { doctorList } from "../components/doctor.js";
import { editor } from "../components/editor.js";
import { icon } from "../components/icons.js";
import { loadProviders, providerPicker } from "../components/providers.js";
import { switchControl } from "../components/switch.js";
import { tagInput } from "../components/tagInput.js";
import { toast } from "../components/toast.js";
import { SECTIONS, USAJOBS_EMAIL } from "./settingsFields.js";
import { KIND_HINTS, renderSources } from "./settingsSources.js";

const TYPE_WORDS = { int: "a whole number", str: "text", bool: "on or off" };
const SPY_OFFSET = 48;
const ICONS_POLL_MS = 15000;

/** A path as one piece per folder, so it wraps between folders and never inside a name. */
const pathText = (path) => path.split(/(?<=\/)/).map((part) => h("span", { class: "path-part", text: part }));

/** @type {{checks: object[], at: number} | null} the newest Doctor result this page load */
let lastDoctor = null;

const FIELDS = SECTIONS.flatMap((section) => section.fields || []);
const KEYS = new Set([...FIELDS.map((field) => field.key), USAJOBS_EMAIL.key, "sources", "watchlist"]);
const FEEDBACK_HELP = "Cairn uses your recent Agree and Too high marks when it ranks new postings.";

const clone = (value) => JSON.parse(JSON.stringify(value));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// a spec's enabled defaults to true, so an explicit true and no flag are the same entry
function specText(spec) {
  const { enabled = true, ...rest } = spec;
  return JSON.stringify({ ...rest, enabled });
}

function sameSetting(key, a, b) {
  if (key !== "sources" && key !== "watchlist") return same(a, b);
  return same((a || []).map(specText), (b || []).map(specText));
}

/**
 * Write a backup zip and say where in a toast.
 * @returns {Promise<{path: string, bytes: number}>}
 */
export async function saveBackup() {
  const made = await api("/api/backup", { method: "POST" });
  toast("Saved a backup to your Downloads folder");
  return made;
}

/** The Settings view on show, and a left view's draft that an Undo is bringing back. */
let shown = null;
let returningDraft = null;

/** The setting a PUT error names, and the message said plainly. */
function readError(message) {
  const text = message.replace(/^request: /, "")
    .replace(/<class '(\w+)'>/g, (_, type) => TYPE_WORDS[type] || type)
    .replace(/(\w+) \| None/g, (_, type) => `${TYPE_WORDS[type] || type} or empty`)
    .replace(/, got NoneType$/, ", got nothing");
  const list = text.match(/^(sources|watchlist)\[\d+\]/);
  if (list) return { key: list[1], text };
  const key = [...text.matchAll(/'(\w+)'/g)].map((m) => m[1]).find((name) => KEYS.has(name));
  return { key: key || null, text };
}

class Settings {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.saved = null;
    this.defaults = {};
    this.draft = null;
    this.errors = {};
    /** @type {Record<string, string>} number fields holding text they refuse */
    this.invalid = {};
    this.saving = false;
    this.mounted = true;
    this.fields = new Map();
    /** @type {Map<string, HTMLButtonElement>} number fields' reset-to-default buttons */
    this.defaultButtons = new Map();
    this.jumped = null;
    this.profileText = "";
    this.kinds = Object.keys(KIND_HINTS);
    this.cleanups = [];
    this.iconsPoll = null;
    this.iconsDone = 0;
    /** the active AI provider's id, or null when it could not be read */
  }

  mount() {
    shown = this;
    this.build();
    this.cleanups.push(register([
      { keys: ["Escape"], label: "Discard unsaved settings", group: "Settings", whileTyping: true, hidden: true,
        run: () => {
          if (this.ctx.overlayOpen() || !this.hasChanges()) return false;
          this.discard();
          return true;
        } },
    ]),
    subscribe("icons_progress", (progress) => {
      this.iconsDone = progress.done;
      this.iconProgress(progress);
    }),
    subscribe("icons_done", ({ error }) => {
      this.iconsDone = 0;
      if (error) toast("Fetching company icons stopped early. Try again later", { tone: "error" });
      this.ctx.refreshStatus();
    }),
    this.ctx.onStatus(() => this.renderIcons()));
    this.load();
    return () => this.unmount();
  }

  unmount() {
    this.mounted = false;
    if (shown === this) shown = null;
    this.cleanups.forEach((fn) => fn());
    clearTimeout(this.iconsPoll);
    if (this.dirtyKeys().length) this.offerReturn();
  }

  /**
   * Say the left-behind draft is gone, with an Undo that brings it back in Settings
   * at the section that was open.
   */
  offerReturn() {
    const draft = clone(this.draft);
    const section = this.nav.querySelector('[aria-current="true"]')?.dataset.section || SECTIONS[0].id;
    const { navigate } = this.ctx;
    toast("Discarded unsaved settings", { undo: () => {
      if (shown) {
        shown.restore(draft);
        shown.jump(section, { focus: true });
        return;
      }
      returningDraft = draft;
      navigate("settings", new URLSearchParams({ section }));
    } });
  }

  restore(draft) {
    if (!this.saved) {
      returningDraft = draft;
      return;
    }
    this.draft = draft;
    this.errors = {};
    this.invalid = {};
    this.renderForm();
  }

  build() {
    this.nav = h("nav", { class: "settings-nav", "aria-label": "Settings sections" },
      SECTIONS.map((section) => h("button", { type: "button", class: "settings-nav-item", "data-section": section.id,
        text: section.title, onclick: () => this.jump(section.id, { focus: true }) })));
    this.sections = new Map(SECTIONS.map((section) => [section.id,
      h("section", { class: "settings-section", id: `settings-${section.id}`, "aria-labelledby": `settings-${section.id}-title`,
        tabindex: "-1" })]));
    this.content = h("div", { class: "settings-content", tabindex: "-1" }, [...this.sections.values()]);
    this.saveCount = h("span", { class: "save-bar-text" });
    this.saveButton = h("button", { type: "button", class: "btn btn-primary btn-sm", text: "Save",
      onclick: () => this.save() });
    this.saveBar = h("div", { class: "save-bar", role: "region", "aria-label": "Unsaved changes", hidden: true },
      this.saveCount,
      h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Discard", title: "Discard (Esc)",
        onclick: () => this.discard() }),
      this.saveButton);
    this.scroller = h("div", { class: "view-scroll settings-scroll" },
      h("div", { class: "settings-layout" }, this.nav, this.content), this.saveBar);
    this.root.replaceChildren(h("section", { class: "view settings-view" },
      h("header", { class: "view-head" }, h("div", { class: "view-heading" }, h("h1", { class: "display", text: "Settings" }))),
      this.scroller));
    for (const section of SECTIONS) {
      this.sections.get(section.id).replaceChildren(this.sectionHead(section), h("p", { class: "muted", text: "Loading…" }));
    }
  }

  sectionHead(section, ...extra) {
    return h("header", { class: "settings-section-head" },
      h("h2", { id: `settings-${section.id}-title`, class: "settings-section-title", text: section.title }), extra);
  }

  async load() {
    const [cfg, profile, schedule, status] = await Promise.all([
      api("/api/settings").catch(() => null),
      api("/api/files/profile").catch(() => null),
      api("/api/schedule", { quiet: true }).catch((error) => ({ error: error.message })),
      // without the status the Advanced section shows dashes for its paths
      api("/api/status", { quiet: true }).catch(() => null)]);
    if (!this.mounted) return;
    this.kinds = status?.kinds || this.ctx.status()?.kinds || this.kinds;
    if (cfg) {
      const { defaults, path, ...values } = cfg;
      this.saved = values;
      this.defaults = defaults || {};
      this.configPath = path;
      this.draft = returningDraft || clone(values);
      returningDraft = null;
      this.renderForm();
    } else {
      for (const id of ["preferences", "sources", "icons", "ranking", "notifications"]) {
        this.sections.get(id).querySelector(".muted").textContent = "Couldn't load the settings.";
      }
    }
    this.renderProfile(profile);
    this.renderSchedule(schedule);
    this.renderAdvanced(status);
    this.renderDoctor();
    this.renderProvider();
    this.renderSystem(status?.version);
    this.watchSections();
    const start = this.ctx.params.get("section");
    const field = this.fields.get(this.ctx.params.get("field") || "")?.wrap;
    if (field) this.showField(field);
    else if (start && this.sections.has(start)) this.jump(start, { focus: true });
    else this.content.focus({ preventScroll: true });
  }

  /** Scroll to one setting and focus its control, for links from the why-nothing list. */
  showField(wrap) {
    const section = wrap.closest(".settings-section");
    this.jumped = section.id.replace("settings-", "");
    this.markNav(this.jumped);
    wrap.scrollIntoView({ block: "center" });
    wrap.classList.add("field-found");
    const control = wrap.querySelector(`#set-${CSS.escape(wrap.dataset.field || "")}`)
      || wrap.querySelector("input, textarea, select") || wrap.querySelector("button");
    control?.focus({ preventScroll: true });
    setTimeout(() => wrap.classList.remove("field-found"), 2000);
  }

  jump(id, { focus = false } = {}) {
    const section = this.sections.get(id);
    this.jumped = id;
    section.scrollIntoView({ block: "start" });
    if (focus) section.focus({ preventScroll: true });
    this.markNav(id);
    this.ctx.setParams(new URLSearchParams({ section: id }), { replace: true });
  }

  markNav(id) {
    for (const item of this.nav.children) {
      if (item.dataset.section === id) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    }
  }

  /**
   * Highlight the last section whose top has scrolled past the top of the view. At
   * the bottom, a short section that was jumped to stays highlighted though its top
   * cannot reach there.
   */
  watchSections() {
    const pick = () => {
      const { scrollTop, clientHeight, scrollHeight } = this.scroller;
      if (scrollTop + clientHeight < scrollHeight - 2) this.jumped = null;
      if (this.jumped) return;
      const top = this.scroller.getBoundingClientRect().top + SPY_OFFSET;
      const passed = SECTIONS.filter((section) => this.sections.get(section.id).getBoundingClientRect().top <= top);
      this.markNav((passed.at(-1) || SECTIONS[0]).id);
    };
    let queued = false;
    this.scroller.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        pick();
      });
    }, { passive: true });
    this.markNav(SECTIONS[0].id);
  }

  // preferences, sources, ranking, notifications
  renderForm() {
    this.fields.clear();
    this.defaultButtons.clear();
    for (const section of SECTIONS.filter((s) => s.fields)) this.renderFields(section);
    renderSources(this);
    this.renderIcons();
    this.renderDirty();
  }

  /** One section of plain fields. Ranking points to AI provider for the models. */
  renderFields(section) {
    const shown = section.fields.filter((field) => MAC || !field.macOnly);
    for (const field of section.fields) {
      this.fields.delete(field.key);
      this.defaultButtons.delete(field.key);
    }
    const elsewhere = section.id === "ranking"
      && h("p", { class: "field-help" }, "Pick the ranking and summary models under ",
        h("a", { class: "link", href: "#settings?section=provider", text: "AI provider", onclick: (event) => {
          event.preventDefault();
          this.jump("provider", { focus: true });
        } }), ".");
    this.sections.get(section.id).replaceChildren(this.sectionHead(section),
      h("div", { class: "fields" }, shown.map((field) => this.field(field))), ...[elsewhere].filter(Boolean));
    if (section.id === "ranking") this.renderFeedbackStats();
  }

  /** "Feedback so far: 12 agree · 3 too high" under the ranking fields. */
  async renderFeedbackStats() {
    const line = h("p", { class: "field-help feedback-stats", text: FEEDBACK_HELP });
    this.sections.get("ranking").append(line);
    // without the counts the line keeps its help text
    const stats = await api("/api/feedback/stats", { quiet: true }).catch(() => null);
    if (!stats || !line.isConnected) return;
    line.replaceChildren(h("strong", { text: `Feedback so far: ${fmt.number(stats.up)} agree · ${fmt.number(stats.down)} too high. ` }),
      FEEDBACK_HELP);
  }

  /** Coverage of company icons, with a button that fetches the rest now. */
  renderIcons() {
    const icons = this.ctx.status()?.icons;
    if (!icons || !this.saved) return;
    if (!this.iconsCard) {
      this.iconsLine = h("p", { class: "icons-coverage" });
      this.iconsButton = h("button", { type: "button", class: "btn btn-sm" }, icon("refresh"), "Fetch icons now");
      this.iconsButton.addEventListener("click", () => this.fetchIcons());
      this.iconsCard = h("div", { class: "icons-card" }, this.iconsLine, this.iconsButton);
      this.sections.get("icons").append(this.iconsCard);
    }
    const off = this.saved.company_icons === false;
    this.iconsButton.disabled = icons.running || off;
    this.iconsButton.title = off ? "Turn on Fetch icons and save first" : null;
    clearTimeout(this.iconsPoll);
    if (icons.running) {
      this.iconProgress({ done: this.iconsDone, remaining: icons.pending });
      // a fetch started from the terminal sends no events here
      this.iconsPoll = setTimeout(() => this.ctx.refreshStatus(), ICONS_POLL_MS);
      return;
    }
    const parts = [`Icons for ${fmt.number(icons.with_icon)} of ${fmt.plural(icons.companies, "company", "companies")}`,
      `${fmt.number(icons.pending)} to look up`];
    if (icons.failed) parts.push(`${fmt.number(icons.failed)} not found`);
    this.iconsLine.textContent = parts.join(" · ");
  }

  async fetchIcons() {
    this.iconsButton.disabled = true;
    try {
      await api("/api/icons", { method: "POST" });
      this.iconProgress({ remaining: this.ctx.status()?.icons?.pending ?? 0 });
    } catch {
      this.iconsButton.disabled = false;
    }
  }

  iconProgress({ done, remaining }) {
    const progress = done ? `${fmt.number(done)} looked up, ${fmt.number(remaining)} to go` : `${fmt.number(remaining)} to go`;
    this.iconsLine?.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), ` Fetching icons… ${progress}`);
  }

  set(key, value) {
    this.draft[key] = value;
    delete this.errors[key];
    this.showError(key);
    this.renderDefault(key);
    this.renderDirty();
  }

  /** A reset button is live only while the field holds something other than its default. */
  renderDefault(key) {
    const button = this.defaultButtons.get(key);
    if (button) button.disabled = !this.invalid[key] && same(this.draft[key], this.defaults[key]);
  }

  setNumber(spec, input) {
    const { value, error } = readNumber(input, spec);
    if (!error) {
      delete this.invalid[spec.key];
      this.set(spec.key, value);
      return;
    }
    this.invalid[spec.key] = error;
    this.showError(spec.key);
    this.renderDefault(spec.key);
    this.renderDirty();
  }

  field(spec) {
    const id = `set-${spec.key}`;
    const value = this.draft[spec.key];
    const help = h("p", { class: "field-help", id: `${id}-help`, text: spec.help });
    const error = h("p", { class: "field-error", id: `${id}-error`, role: "alert", hidden: true });
    let control;
    if (spec.type === "tags") {
      control = tagInput(value || [], { id, onChange: (tags) => this.set(spec.key, tags) });
    } else if (spec.type === "switch") {
      control = switchControl(null, Boolean(value), (on) => this.set(spec.key, on), { id, key: spec.key });
    } else if (spec.type === "choice") {
      control = h("select", { class: "select", id, onchange: (event) => this.set(spec.key, event.target.value) },
        spec.choices.map(([choice, label]) => h("option", { value: choice, selected: choice === value, text: label })));
    } else if (spec.type === "number") {
      const input = h("input", { type: "number", id, class: "input input-num", value: value ?? "",
        min: spec.min, max: spec.max, step: "1", placeholder: spec.nullable ? "none" : null,
        oninput: () => this.setNumber(spec, input) });
      const fallback = this.defaults[spec.key];
      const shown = fallback ?? "none";
      const reset = h("button", { type: "button", class: "default-btn mono", "data-key": `default-${spec.key}`,
        title: `Reset to the default, ${shown}`, "aria-label": `Reset ${spec.label.toLowerCase()} to the default, ${shown}`,
        text: `default ${shown}`, onclick: () => {
          input.value = fallback ?? "";
          this.setNumber(spec, input);
          input.focus();
        } });
      this.defaultButtons.set(spec.key, reset);
      control = h("div", { class: "number-input" }, input, spec.unit && h("span", { class: "unit", text: spec.unit }),
        spec.key in this.defaults && reset);
    } else {
      control = h("input", { type: "text", id, class: "input input-text", value: value ?? "", spellcheck: "false",
        autocomplete: "off", oninput: (event) => this.set(spec.key, event.target.value) });
    }
    control.querySelector?.(`#${id}`)?.setAttribute("aria-describedby", `${id}-help ${id}-error`);
    if (control.id === id) control.setAttribute("aria-describedby", `${id}-help ${id}-error`);
    const wrap = h("div", { class: `field field-${spec.type}`, "data-field": spec.key },
      h("label", { class: "field-label", for: id, text: spec.label }), help, control, error);
    this.fields.set(spec.key, { wrap, error });
    this.showError(spec.key);
    this.renderDefault(spec.key);
    return wrap;
  }

  showError(key) {
    const field = this.fields.get(key);
    if (!field) return;
    const message = this.invalid[key] || this.errors[key];
    field.error.hidden = !message;
    field.error.textContent = message || "";
    field.wrap.classList.toggle("has-error", Boolean(message));
    field.wrap.querySelector(`#set-${key}`)?.setAttribute("aria-invalid", String(Boolean(message)));
  }

  dirtyKeys() {
    if (!this.draft) return [];
    return Object.keys(this.draft).filter((key) => !sameSetting(key, this.draft[key], this.saved[key]));
  }

  hasChanges() {
    return this.dirtyKeys().length > 0 || Object.keys(this.invalid).length > 0;
  }

  renderDirty() {
    const n = this.dirtyKeys().length;
    const bad = Object.keys(this.invalid).length;
    this.saveBar.hidden = n === 0 && bad === 0;
    this.saveButton.disabled = bad > 0 || this.saving;
    const changes = n === 1 ? "1 unsaved change" : `${n} unsaved changes`;
    const fixes = bad === 1 ? "1 field needs a fix" : `${bad} fields need a fix`;
    this.saveCount.textContent = [n && changes, bad && fixes].filter(Boolean).join(" · ");
  }

  discard() {
    const focusedKey = document.activeElement?.closest?.("[data-field]")?.dataset.field;
    this.draft = clone(this.saved);
    this.errors = {};
    this.invalid = {};
    this.renderForm();
    const target = focusedKey && (this.content.querySelector(`#set-${focusedKey}`)
      || this.content.querySelector(`[data-field="${focusedKey}"] input, [data-field="${focusedKey}"] button`));
    (target || this.content).focus({ preventScroll: true });
    toast("Discarded unsaved settings");
  }

  async save() {
    const keys = this.dirtyKeys();
    if (!keys.length || Object.keys(this.invalid).length) return;
    const body = Object.fromEntries(keys.map((key) => [key, this.draft[key]]));
    this.saving = true;
    this.renderDirty();
    try {
      const saved = await api("/api/settings", { method: "PUT", body, quiet: true });
      toast(keys.length === 1 ? "Saved 1 setting" : `Saved ${keys.length} settings`);
      this.ctx.refreshStatus();
      this.saved = saved;
      this.draft = clone(saved);
      this.errors = {};
      if (this.mounted) this.renderForm();
    } catch (error) {
      const { key, text } = readError(error.message);
      toast(`Couldn't save: ${text}`, { tone: "error" });
      if (key && this.mounted) {
        this.errors[key] = text;
        this.showError(key);
        const field = this.fields.get(key)?.wrap;
        field?.scrollIntoView({ block: "center" });
        field?.querySelector("input, button")?.focus({ preventScroll: true });
      }
    } finally {
      this.saving = false;
      if (this.mounted) this.renderDirty();
    }
  }

  // profile, schedule, doctor, advanced
  renderProfile(file) {
    const section = SECTIONS[0];
    const box = this.sections.get("profile");
    const redo = h("button", { type: "button", class: "btn btn-sm", text: "Run setup again",
      title: "Draft your profile from a resume again, then go over your preferences, companies and phone alerts",
      onclick: () => this.ctx.navigate("setup") });
    const retake = h("button", { type: "button", class: "btn btn-sm", text: "Retake the questions",
      title: "Answer the setup questions again without a new resume",
      onclick: () => this.ctx.navigate("setup", new URLSearchParams({ retake: "1" })) });
    const actions = h("div", { class: "section-actions" }, retake, redo);
    if (!file) {
      box.replaceChildren(this.sectionHead(section, actions), h("p", { class: "muted", text: "Couldn't load your profile." }));
      return;
    }
    this.profileText = file.text;
    const save = h("button", { type: "button", class: "btn btn-primary btn-sm", text: "Save profile", disabled: true });
    const discard = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Discard", disabled: true });
    const edit = editor(file.text, { label: "Profile", onInput: (text) => {
      save.disabled = discard.disabled = text === this.profileText;
    } });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        const written = await api("/api/files/profile", { method: "PUT", body: { text: edit.value() } });
        toast("Saved your profile");
        if (!this.mounted) return;
        this.profileText = written.text;
        discard.disabled = true;
      } catch {
        save.disabled = false;
      }
    });
    discard.addEventListener("click", () => {
      edit.setValue(this.profileText);
      save.disabled = discard.disabled = true;
    });
    box.replaceChildren(this.sectionHead(section, actions),
      h("p", { class: "field-help", text: "Cairn compares every job to this profile. Lines that start with TODO still need your answer." }),
      edit.element,
      h("div", { class: "section-actions" }, save, discard));
  }

  renderSchedule(schedule) {
    const section = SECTIONS.find((s) => s.id === "schedule");
    const box = this.sections.get("schedule");
    const installed = Boolean(schedule?.installed);
    const at = (h0, m0) => `${String(h0).padStart(2, "0")}:${String(m0 ?? 0).padStart(2, "0")}`;
    const status = schedule?.error && !installed ? "Couldn't read the schedule. Set the time again below."
      : installed ? `On, daily at ${schedule.hour == null ? "an unknown time" : at(schedule.hour, schedule.minute)}${schedule.loaded ? "" : " (not running yet)"}${schedule.hourly ? "" : ". The hourly check is off; press Change time to turn it on."}`
        : "Off";
    const hour = h("input", { type: "number", id: "schedule-hour", class: "input input-num", min: "0", max: "23",
      value: String(schedule?.hour ?? 7) });
    const minute = h("input", { type: "number", id: "schedule-minute", class: "input input-num", min: "0", max: "59",
      value: String(schedule?.minute ?? 0) });
    const act = async (button, path, body) => {
      button.disabled = true;
      try {
        const next = await api(path, { method: "POST", body });
        toast(next.installed ? `Scheduled the daily run for ${at(next.hour, next.minute)}` : "Turned off the daily run");
        this.ctx.refreshStatus();
        if (this.mounted) this.renderSchedule(next);
      } catch {
        button.disabled = false;
      }
    };
    const install = h("button", { type: "button", class: "btn btn-primary btn-sm", text: installed ? "Change time" : "Turn on" });
    install.addEventListener("click", () => act(install, "/api/schedule/install",
      { hour: Number.parseInt(hour.value, 10), minute: Number.parseInt(minute.value, 10) }));
    const remove = installed ? h("button", { type: "button", class: "btn btn-sm", text: "Turn off" }) : null;
    remove?.addEventListener("click", () => act(remove, "/api/schedule/remove"));
    box.replaceChildren(this.sectionHead(section),
      h("p", { class: "field-help", text: "Cairn runs once a day at this time, and checks the companies you follow every hour, while you're logged in to this computer." }),
      h("p", { class: `schedule-status${installed ? (schedule.loaded ? " is-on" : " is-off") : ""}` }, h("span", { class: "run-dot", "aria-hidden": "true" }), status),
      h("div", { class: "add-row" },
        h("label", { class: "field-inline", for: "schedule-hour" }, h("span", { class: "field-inline-label", text: "Hour" }), hour),
        h("label", { class: "field-inline", for: "schedule-minute" }, h("span", { class: "field-inline-label", text: "Minute" }), minute),
        install, remove));
  }

  /** The Doctor section with the last result, if any; checks run only on request. */
  renderDoctor() {
    const section = SECTIONS.find((s) => s.id === "doctor");
    const run = h("button", { type: "button", class: "btn btn-sm", onclick: () => this.runDoctor() },
      icon("refresh"), "Run checks");
    const body = h("div", { class: "doctor-body" }, lastDoctor
      ? [h("p", { class: "field-help", text: `Last checked ${fmt.when(new Date(lastDoctor.at).toISOString())}` }),
        doctorList(lastDoctor.checks)]
      : h("p", { class: "muted", text: "Not checked yet." }));
    this.doctor = { run, body };
    this.sections.get("doctor").replaceChildren(this.sectionHead(section, run),
      h("p", { class: "field-help", text: "Checks that Cairn has what it needs to run." }), body);
  }

  async runDoctor() {
    const { run, body } = this.doctor;
    run.disabled = true;
    body.setAttribute("aria-busy", "true");
    body.replaceChildren(h("p", { class: "muted" }, h("span", { class: "spinner", "aria-hidden": "true" }),
      " Checking this computer…"));
    try {
      const { checks } = await api("/api/doctor", { method: "POST", quiet: true });
      lastDoctor = { checks, at: Date.now() };
      if (this.mounted) this.renderDoctor();
    } catch (error) {
      body.replaceChildren(h("p", { class: "field-error", text: "Couldn't run the checks. Try again." }));
    } finally {
      body.removeAttribute("aria-busy");
      run.disabled = false;
    }
  }

  async renderProvider() {
    const section = SECTIONS.find((s) => s.id === "provider");
    const box = this.sections.get("provider");
    let data;
    try {
      data = await loadProviders();
    } catch (error) {
      box.replaceChildren(this.sectionHead(section), h("p", { class: "field-error", text: "Couldn't load the AI providers. Reload the page to try again." }));
      return;
    }
    if (!this.mounted) return;
    const picker = providerPicker(data, { onSaved: () => {
      if (!this.mounted) return;
      this.runDoctor();
    } });
    box.replaceChildren(this.sectionHead(section),
      h("p", { class: "field-help", text: "Cairn uses the AI provider you pick here to rank postings and write summaries. Its Save button applies the change right away." }),
      picker.element);
  }

  renderSystem(version) {
    const section = SECTIONS.find((s) => s.id === "system");
    const login = h("div", { class: "field" }, h("span", { class: "field-label", text: "Start at login" }),
      h("p", { class: "field-help", text: MAC ? "Opens Cairn in the menu bar when you log in to this Mac."
        : "Opens Cairn when you log in to this computer." }),
      h("p", { class: "muted", text: "Checking…" }));
    this.renderAutostart(login);
    const updateLine = h("p", { class: "system-line" });
    const check = h("button", { type: "button", class: "btn btn-sm" }, icon("refresh"), "Check now");
    const showUpdate = (info, asked) => {
      const parts = [h("span", { text: `You have ${version ? `version ${version}` : "an unknown version"}` })];
      if (info?.newer) {
        const url = safeUrl(info.url);
        parts.push(" · ", url ? h("a", { class: "link", href: url, target: "_blank", rel: "noopener noreferrer",
          text: `Update available: ${info.latest}` }) : h("strong", { text: `Update available: ${info.latest}` }));
      } else if (info) parts.push(" · up to date");
      else if (asked) parts.push(" · couldn't check for updates");
      updateLine.replaceChildren(...parts);
    };
    showUpdate(null, false);
    check.addEventListener("click", async () => {
      check.disabled = true;
      const info = await this.ctx.checkUpdate();
      check.disabled = false;
      if (this.mounted) showUpdate(info, true);
    });
    const saved = h("p", { class: "field-help" });
    const backup = h("button", { type: "button", class: "btn btn-sm" }, icon("download"), "Save a backup");
    backup.addEventListener("click", async () => {
      backup.disabled = true;
      try {
        const made = await saveBackup();
        saved.replaceChildren(`Last backup: ${made.path.split("/").pop()} (${fmt.number(Math.round(made.bytes / 1024))} KB)`);
      } catch {
        // saveBackup has shown the error
      } finally {
        backup.disabled = false;
      }
    });
    this.sections.get("system").replaceChildren(this.sectionHead(section),
      h("div", { class: "fields" },
        login,
        h("div", { class: "field" }, h("span", { class: "field-label", text: "Updates" }),
          h("div", { class: "system-row" }, updateLine, check)),
        h("div", { class: "field" }, h("span", { class: "field-label", text: "Backup" }),
          h("p", { class: "field-help", text: "Saves your profile, settings, jobs and applications to a file in your Downloads folder." }),
          h("div", { class: "system-row" }, backup), saved),
        this.restoreField(),
        h("div", { class: "field" }, h("span", { class: "field-label", text: "Settings file" }),
          h("p", { class: "field-help", text: "Download your settings as a file." }),
          h("div", { class: "system-row" }, h("a", { class: "btn btn-sm", href: "/api/settings/export", download: "config.toml" },
            icon("download"), "Export settings")))));
  }

  async renderAutostart(box, next) {
    const head = [...box.children].slice(0, 2);
    let state;
    try {
      state = next || await api("/api/autostart", { quiet: true });
    } catch (error) {
      box.replaceChildren(...head, h("p", { class: "field-error", text: "Couldn't check whether Cairn opens at login." }));
      return;
    }
    if (!this.mounted) return;
    const toggle = switchControl("Open Cairn when I log in", state.installed, async (on) => {
      try {
        const changed = await api(`/api/autostart/${on ? "install" : "remove"}`, { method: "POST" });
        toast(on ? "Cairn opens when you log in" : "Cairn no longer opens at login");
        this.renderAutostart(box, changed);
      } catch {
        this.renderAutostart(box);
      }
    }, { key: "autostart" });
    box.replaceChildren(...head, toggle);
    if (state.error) box.append(h("p", { class: "field-error", text: state.error }));
  }

  restoreField() {
    const note = h("div", { class: "restore-confirm", hidden: true });
    const closeNote = () => {
      note.hidden = true;
      note.replaceChildren();
      choose.focus();
    };
    note.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || note.hidden) return;
      event.preventDefault();
      closeNote();
    });
    const input = h("input", { type: "file", accept: ".zip,application/zip", hidden: true, "aria-label": "Backup file to restore" });
    const choose = h("button", { type: "button", class: "btn btn-sm", onclick: () => input.click() },
      icon("upload"), "Restore a backup…");
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      input.value = "";
      if (!file) return;
      const go = h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Restore" });
      const cancel = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Cancel", onclick: closeNote });
      // a backup that switches the AI provider comes back as a 409 listing the changes
      const restore = async (button, acceptModelChange) => {
        const label = button.textContent;
        button.disabled = true;
        button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Restoring…");
        const body = new FormData();
        body.append("backup", file, file.name);
        try {
          const path = acceptModelChange ? "/api/restore?accept_model_change=true" : "/api/restore";
          const done = await api(path, { method: "POST", body, quiet: true });
          setPref("restored", done.previous);
          location.reload();
        } catch (error) {
          button.disabled = false;
          button.textContent = label;
          if (error.body?.model_changes) confirmModelChange(error.body.model_changes);
          else toast(error.message, { tone: "error" });
        }
      };
      const confirmModelChange = (changes) => {
        const labels = { llm_provider: "AI provider", llm_base_url: "Address", claude_bin: "Claude program" };
        const again = h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Restore with these" });
        again.addEventListener("click", () => restore(again, true));
        const back = h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Cancel", onclick: closeNote });
        note.replaceChildren(h("p", {}, h("strong", { text: "This backup switches your AI provider. " }),
          "Cairn would send your profile and postings to the one below, so check it before you restore."),
        h("dl", { class: "facts" }, ...changes.flatMap((change) => [
          h("dt", { text: labels[change.setting] || change.setting }),
          h("dd", { class: "mono", text: `${change.current || "–"} → ${change.incoming || "–"}` })])),
        h("div", { class: "section-actions" }, again, back));
        back.focus();
      };
      go.addEventListener("click", () => restore(go, false));
      note.replaceChildren(h("p", {}, h("strong", { text: `Restore ${file.name}? ` }),
        "It replaces your settings, profile, jobs and applications. Cairn keeps a copy of your current data.",
        this.hasChanges() ? " You lose any unsaved changes on this page." : ""),
      h("div", { class: "section-actions" }, go, cancel));
      note.hidden = false;
      cancel.focus();
    });
    return h("div", { class: "field" }, h("span", { class: "field-label", text: "Restore" }),
      h("p", { class: "field-help", text: "Replaces your data with a saved backup. Cairn reloads when it's done." }),
      h("div", { class: "system-row" }, input, choose), note);
  }

  renderAdvanced(status) {
    const section = SECTIONS.find((s) => s.id === "advanced");
    const pathRow = (label, value, noun) => [h("dt", { text: label }), h("dd", {},
      value ? h("button", { type: "button", class: "mono copy-id", title: "Copy", onclick: () => copyText(value, noun) },
        h("span", {}, pathText(value)), icon("copy")) : "–")];
    this.sections.get("advanced").replaceChildren(this.sectionHead(section),
      h("dl", { class: "facts advanced-facts" },
        pathRow("Home folder", status?.home, "the home folder"),
        pathRow("Config file", this.configPath, "the config path"),
        pathRow("Database", status?.database, "the database path"),
        h("dt", { text: "Version" }), h("dd", { class: "mono", text: status?.version || "–" })),
      h("p", { class: "field-help", text: "config.toml holds only the settings you changed from the defaults. After you edit it by hand, restart the app to load the changes." }));
  }
}

/**
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Settings(root, ctx).mount();
}
