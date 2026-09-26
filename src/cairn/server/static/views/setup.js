import { api } from "../lib/api.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { withRole } from "../lib/roles.js";
import { doctorList } from "../components/doctor.js";
import { editor } from "../components/editor.js";
import { activeFirstRun, FirstRunScreen, launchFrame } from "../components/firstRun.js";
import { icon } from "../components/icons.js";
import { pixelArt } from "../components/pixelart.js";
import { loadProviders, providerPicker } from "../components/providers.js";
import { stepper } from "../components/stepper.js";
import { switchControl } from "../components/switch.js";
import { tagInput } from "../components/tagInput.js";
import { toast } from "../components/toast.js";

const STEPS = ["AI", "Resume", "Review", "Preferences", "Companies", "Alerts"];
const [AI_STEP, RESUME_STEP, REVIEW_STEP, PREFS_STEP, COMPANIES_STEP, ALERTS_STEP] = STEPS.keys();
const CLAUDE_CODE = "claude-code";
const CLAUDE_CODE_URL = "https://claude.com/claude-code";
const ROLE_LABELS = { backend: "Backend", frontend: "Frontend", fullstack: "Full stack", systems: "Systems",
  ml: "Machine learning", data: "Data", quant: "Quant", research: "Research", platform: "Platform",
  security: "Security", mobile: "Mobile", embedded: "Embedded" };
const ANCHOR_HINTS = ["e.g. Stripe, Databricks, Jane Street = 90", "e.g. Datadog, Snowflake = 75",
  "e.g. a strong regional company = 60"];
const ACCEPT = ".pdf,.txt,.md";
// what /api/onboard/options answers, for when it cannot be reached; without the
// role keywords there are no role checkboxes
const BUILT_IN_OPTIONS = { roles: {}, work_authorization: ["US citizen", "F-1 OPT", "needs sponsorship", "unknown"] };
const GRADUATION_MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;
const DEFAULT_GRADUATION_MONTH = "06";
const DEFAULT_MAX_YEARS = 2;
const READ_LINE = /^\[setup\] read (\d+) words/;
const SLOW_DRAFT_SECONDS = 60;
// long enough to see the draft pass its check before Review replaces the screen
const CHECKED_PAUSE_MS = 700;

const roleLabel = (role) => ROLE_LABELS[role] || role[0].toUpperCase() + role.slice(1);

/**
 * The graduation year in a month field, or why the field refuses its text. The
 * field is plain text where the browser has no month picker.
 */
function readGraduation(input) {
  const raw = input.value.trim();
  if (input.validity.badInput) return { error: "Enter a month and a year" };
  if (!raw) return { year: null };
  const match = raw.match(GRADUATION_MONTH);
  if (!match) return { error: "Enter the month as YYYY-MM" };
  const year = Number(match[1]);
  if (year < 2000 || year > 2100) return { error: "Enter a year from 2000 to 2100" };
  return { year };
}

/** A 0-50 number box; an empty one reports null. */
function yearsInput(id, value, label, onChange) {
  return h("input", { type: "number", class: "input input-num", id, min: "0", max: "50", step: "1",
    value: value ?? "", "aria-label": label, oninput: (event) => {
      const n = event.target.valueAsNumber;
      onChange(Number.isInteger(n) && n >= 0 && n <= 50 ? n : null);
    } });
}

function sizeText(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

class Setup {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.step = 0;
    this.file = null;
    this.text = "";
    this.overwrite = false;
    this.conflict = null;
    this.error = null;
    /** a neutral line under the resume step, such as the note that drafting stopped */
    this.notice = null;
    this.stoppedAt = null;
    this.stopDraftScreen = null;
    this.runScreen = null;
    this.draft = null;
    this.profile = "";
    this.prefs = null;
    this.options = null;
    this.starters = [];
    /** what the settings held when the wizard opened, so Finish sends only what changed */
    this.initial = { ntfy_topic: "", watchlist: [] };
    this.graduation = "";
    this.graduationError = null;
    this.result = null;
    this.timer = null;
    this.mounted = true;
    /**
     * The AI step: Claude Code or another provider, the provider list, and whether
     * the other provider answered a test on its current values and saved. One
     * picker lives for the whole wizard, so its drafts and typed key survive Back.
     */
    this.ai = { mode: null, data: null, error: null, loading: false, tested: false, saved: false };
    this.picker = null;
  }

  mount() {
    this.card = h("div", { class: "setup-card" });
    this.scroll = h("div", { class: "view-scroll setup-scroll" });
    this.root.replaceChildren(h("section", { class: "view setup-view" }, this.scroll));
    this.open();
    return () => {
      this.mounted = false;
      clearInterval(this.timer);
      this.stopDraftScreen?.();
      this.runScreen?.stop();
    };
  }

  /** Show the wizard, or the run screen when a first run is already going. */
  async open() {
    const run = await activeFirstRun();
    if (!this.mounted) return;
    if (run) {
      this.followRun().adopt(run);
      return;
    }
    this.showCard();
    this.render();
    this.card.querySelector("h1")?.focus?.();
  }

  /** Put a full-panel screen in place of the wizard card. @param {HTMLElement} element */
  showScreen(element) {
    this.scroll.classList.add("launch-scroll");
    this.scroll.replaceChildren(element);
    this.scroll.scrollTop = 0;
    element.querySelector("h1")?.focus({ preventScroll: true });
  }

  showCard({ reveal = false } = {}) {
    this.scroll.classList.remove("launch-scroll");
    this.card.classList.toggle("is-revealed", reveal);
    this.scroll.replaceChildren(this.card);
  }

  render() {
    const done = this.result != null;
    const body = done ? this.finished()
      : [this.aiStep, this.resumeStep, this.reviewStep, this.prefsStep, this.companiesStep, this.alertsStep][this.step].call(this);
    this.card.replaceChildren(
      h("header", { class: "setup-head" }, pixelArt("checklist"),
        h("h1", { class: "display", tabindex: "-1", text: done ? "You are set up" : "Set up Cairn" }),
        h("p", { class: "setup-sub", text: done ? "Setup saved your profile and preferences."
          : "Pick an AI provider and add your resume. Cairn drafts a profile from it for you to check, then you set a few preferences." })),
      stepper(STEPS, done ? STEPS.length : this.step, { label: "Setup steps" }),
      body);
  }

  /** The active provider's label from /api/llm, once the AI step has loaded it. */
  providerLabel() {
    const id = this.ai.data?.current.provider;
    return this.ai.data?.providers.find((p) => p.id === id)?.label || "The AI provider";
  }

  go(step) {
    this.step = step;
    this.render();
    this.card.querySelector("h1").focus({ preventScroll: true });
    this.card.closest(".setup-scroll").scrollTop = 0;
  }

  // step 2, the resume
  resumeStep() {
    const input = h("input", { type: "file", accept: ACCEPT, class: "visually-hidden", id: "resume-file",
      onchange: () => input.files[0] && this.pickFile(input.files[0]) });
    const zone = h("label", { class: `dropzone${this.file ? " has-file" : ""}`, for: "resume-file" },
      icon("upload"),
      this.file ? h("span", { class: "dropzone-file" }, h("strong", { text: this.file.name }),
        h("span", { class: "muted", text: ` · ${sizeText(this.file.size)}` }))
        : h("span", {}, h("strong", { text: "Drop your resume here" }), " or click to choose a file"),
      h("span", { class: "dropzone-hint", text: "PDF, TXT or Markdown, up to 10 MB" }));
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
      const file = event.dataTransfer?.files?.[0];
      if (file) this.pickFile(file);
    });
    const paste = h("textarea", { class: "note setup-paste", id: "resume-text", rows: "8",
      placeholder: "…or paste the resume text here", "aria-label": "Resume text" });
    paste.value = this.text;
    paste.addEventListener("paste", (event) => {
      const file = event.clipboardData?.files?.[0];
      if (!file) return;
      event.preventDefault();
      this.pickFile(file);
    });
    const start = h("button", { type: "button", class: "btn btn-primary", disabled: !this.file && !this.text.trim(),
      text: "Draft my profile", onclick: () => this.draftProfile() });
    paste.addEventListener("input", () => {
      this.text = paste.value;
      start.disabled = !this.file && !this.text.trim();
    });
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn reads your resume and writes a profile from it." }),
      input, zone,
      this.file && h("button", { type: "button", class: "link-btn", text: "Use pasted text instead",
        onclick: () => {
          this.file = null;
          this.render();
        } }),
      paste,
      this.conflict && this.conflictBox(() => this.draftProfile()),
      this.error && h("p", { class: "field-error", role: "alert", text: this.error }),
      h("div", { class: "setup-actions" },
        h("button", { type: "button", class: "btn btn-ghost", text: "Back", onclick: () => this.go(AI_STEP) }),
        h("p", { class: "setup-progress", role: "status", text: this.notice || "" }),
        h("span", { class: "filter-spacer" }), start));
  }

  pickFile(file) {
    if (!/\.(pdf|txt|md)$/i.test(file.name)) {
      toast(`${file.name} is not a PDF, TXT or Markdown file`, { tone: "error" });
      return;
    }
    this.file = file;
    this.error = null;
    this.render();
  }

  conflictBox(retry) {
    return h("div", { class: "conflict", role: "alert" },
      h("p", {}, h("strong", { text: "You already have a profile. " }),
        "Setting up again replaces it and your preferences."),
      h("div", { class: "setup-actions" },
        h("button", { type: "button", class: "btn btn-danger", text: "Overwrite", onclick: () => {
          this.overwrite = true;
          this.conflict = null;
          retry();
        } }),
        h("button", { type: "button", class: "btn btn-ghost", text: "Keep mine", onclick: () => this.ctx.navigate("settings") })));
  }

  async draftProfile() {
    this.error = null;
    this.notice = null;
    this.conflict = null;
    const abort = new AbortController();
    const screen = this.draftingScreen(abort);
    this.showScreen(screen.element);
    let body;
    if (this.file) {
      body = new FormData();
      body.append("resume", this.file, this.file.name);
      if (this.overwrite) body.append("overwrite", "true");
    } else {
      body = { text: this.text, overwrite: this.overwrite };
    }
    try {
      const [draft, current] = await Promise.all([
        api("/api/onboard/draft", { method: "POST", body, quiet: true, signal: abort.signal }),
        // without the current settings the preferences start from the draft's suggestions
        api("/api/settings", { quiet: true }).catch(() => null),
        this.loadOptions()]);
      this.draft = draft;
      this.profile = draft.profile_md;
      this.prefs = this.startingPrefs(draft.suggestions, current);
      this.step = REVIEW_STEP;
      await screen.checked();
    } catch (error) {
      if (abort.signal.aborted) {
        this.stoppedAt = Date.now();
        this.notice = "Drafting stopped. Nothing was saved.";
      } else if (error.status === 409 && error.body?.files) this.conflict = this.conflictFiles(error);
      else if (error.status === 409 && this.stoppedAt) {
        this.error = "The draft you stopped is still finishing. Try again in a minute.";
      } else this.error = `Couldn't draft the profile: ${error.message}`;
    } finally {
      this.stopDraftScreen();
    }
    if (!this.mounted) return;
    this.showCard({ reveal: this.step === REVIEW_STEP });
    this.go(this.step);
  }

  /**
   * The screen shown while the profile drafts: the resume read, the model writing,
   * the draft checked, each marked as the server reports it.
   * @param {AbortController} abort  aborted by Cancel
   */
  draftingScreen(abort) {
    const started = Date.now();
    const provider = this.providerLabel();
    const frame = launchFrame({ scene: "reading", title: "Reading your resume",
      lead: `Cairn is drafting your profile from ${this.file ? this.file.name : "the text you pasted"}.`,
      steps: [["read", "Read the resume"], ["write", "Write your profile"], ["check", "Check the draft"]] });
    frame.step("read", "now", this.file ? sizeText(this.file.size) : fmt.plural(this.text.trim().length, "character"));
    frame.step("write", "todo");
    frame.step("check", "todo");
    frame.actions.append(h("button", { type: "button", class: "btn", text: "Cancel",
      title: "Stop drafting the profile", onclick: () => abort.abort() }));
    const tick = () => {
      const seconds = (Date.now() - started) / 1000;
      frame.clock.textContent = `${fmt.duration(seconds)} so far. ${seconds < SLOW_DRAFT_SECONDS
        ? "This usually takes 20 to 60 seconds." : "This is taking longer than usual."}`;
    };
    tick();
    this.timer = setInterval(tick, 1000);
    let read = false;
    const markRead = (words) => {
      read = true;
      frame.step("read", "done", words == null ? "" : fmt.plural(words, "word"));
      frame.step("write", "now", provider);
    };
    const unsubscribe = subscribe("info", ({ text }) => {
      const found = typeof text === "string" && text.match(READ_LINE);
      if (!found || read) return;
      markRead(Number(found[1]));
      frame.say(`Read ${fmt.plural(Number(found[1]), "word")}. Cairn is writing your profile.`);
    });
    this.stopDraftScreen = () => {
      clearInterval(this.timer);
      unsubscribe();
    };
    return {
      element: frame.element,
      /** Mark the draft received and checked, and hold the screen a moment. */
      checked: async () => {
        this.stopDraftScreen();
        if (!read) markRead(null);
        frame.step("write", "done", fmt.duration((Date.now() - started) / 1000));
        frame.step("check", "done");
        frame.actions.replaceChildren();
        frame.say("Your profile draft is ready.");
        await new Promise((resolve) => setTimeout(resolve, CHECKED_PAUSE_MS));
      },
    };
  }

  /** The role and authorization choices, kept once known; a failure falls back to the built-in ones. */
  async loadOptions() {
    [this.options, this.starters] = await Promise.all([
      this.options ?? api("/api/onboard/options", { quiet: true }).catch(() => {
        toast("Couldn't load the setup choices, so these are the defaults", { tone: "error" });
        return BUILT_IN_OPTIONS;
      }),
      this.starters.length ? this.starters : api("/api/watchlist/starters", { quiet: true }).catch(() => [])]);
  }

  conflictFiles(error) {
    return error.body?.files || [error.message];
  }

  /**
   * The answers to start from: the draft's suggestions over what the settings
   * already hold, so setting up again keeps the topic, watchlist and the rest.
   */
  startingPrefs(suggestions, current) {
    const either = (suggested, saved) => (suggested?.length ? suggested : saved || []);
    const isRemote = (place) => place.toLowerCase() === "remote";
    const allow = current?.location_allow || [];
    const watchlist = (current?.watchlist || []).map((spec) => spec.company || spec.location);
    const year = suggestions.graduation_year ?? current?.graduation_year ?? null;
    const month = suggestions.graduation_year != null && suggestions.graduation_month
      ? String(suggestions.graduation_month).padStart(2, "0") : DEFAULT_GRADUATION_MONTH;
    this.graduation = year == null ? "" : `${year}-${month}`;
    this.initial = { ntfy_topic: current?.notify_ntfy_topic || "", watchlist };
    this.roles = suggestions.roles || [];
    return {
      title_keywords: suggestions.title_keywords ?? current?.title_keywords ?? [],
      title_exclude: suggestions.title_exclude ?? current?.title_exclude ?? [],
      title_exclude_field: suggestions.title_exclude_field ?? current?.title_exclude_field ?? [],
      locations: either(suggestions.locations, allow.filter((place) => !isRemote(place))),
      remote_ok: allow.some(isRemote),
      us_only: current?.us_only ?? false,
      graduation_year: year,
      experience_years: current?.experience_years ?? 0,
      max_years_required: current ? current.max_years_required : DEFAULT_MAX_YEARS,
      degrees_held: either(suggestions.degrees_held, current?.degrees_held),
      internship_terms: either(suggestions.internship_terms, current?.wanted_intern_terms),
      work_authorization: suggestions.work_authorization || "unknown",
      calibre_anchors: ["", "", ""],
      ntfy_topic: this.initial.ntfy_topic,
      watchlist: [...watchlist],
      starter_watchlists: [],
    };
  }

  // step 3, review profile.md
  reviewStep() {
    const edit = editor(this.profile, { label: "Profile draft", onInput: (text) => { this.profile = text; } });
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn compares every job to this profile. Fix anything it got wrong. The next step asks about locations and the companies you rate highest." }),
      edit.element,
      h("div", { class: "setup-actions" },
        h("button", { type: "button", class: "btn btn-ghost", text: "Back", onclick: () => this.go(RESUME_STEP) }),
        h("span", { class: "filter-spacer" }),
        h("button", { type: "button", class: "btn btn-primary", text: "Next: preferences", onclick: () => this.go(PREFS_STEP) })));
  }

  // step 1, the AI provider
  /** @returns {boolean} whether Claude Code is on this Mac, as /api/llm/providers reports */
  claudeFound() {
    return Boolean(this.ai.data?.providers.find((p) => p.id === CLAUDE_CODE)?.configured);
  }

  /**
   * @returns {boolean} whether the AI step may go on: Claude Code picked and found,
   * or another provider answered a test on its current values, which Next then saves
   */
  aiReady() {
    const ai = this.ai;
    return ai.mode === "claude" ? this.claudeFound() : ai.mode === "other" && ai.tested;
  }

  /** @returns {boolean} whether setup may finish: the AI step's choice is ready and saved */
  aiDone() {
    return this.aiReady() && (this.ai.mode === "claude" || this.ai.saved);
  }

  /** The provider list and the current choice, loaded again on Try again. */
  async loadAi() {
    const ai = this.ai;
    if (ai.data || ai.loading) return;
    ai.loading = true;
    ai.error = null;
    try {
      ai.data = await loadProviders({ keys: false });
    } catch (error) {
      ai.error = error.message;
    } finally {
      ai.loading = false;
    }
    if (ai.data) {
      const current = ai.data.current.provider;
      ai.mode ??= current !== CLAUDE_CODE ? "other" : this.claudeFound() ? "claude" : null;
    }
    if (this.mounted && this.step === AI_STEP) this.render();
  }

  /** The one picker for the providers other than Claude Code, made on first use. */
  otherPicker() {
    const ai = this.ai;
    this.picker ??= providerPicker(ai.data, { requireTest: true,
      only: ai.data.providers.map((p) => p.id).filter((id) => id !== CLAUDE_CODE),
      onChange: () => {
        ai.tested = false;
        ai.saved = false;
        this.syncAiNext?.();
      },
      onTested: (ok) => {
        ai.tested = ok;
        this.syncAiNext?.();
      },
      onSaved: (current) => {
        ai.data.current = current;
        ai.saved = true;
        this.syncAiNext?.();
      } });
    return this.picker.element;
  }

  claudeNote() {
    const ai = this.ai;
    if (!ai.data) return h("span", { class: "provider-notes", text: "Looking for Claude Code on this computer…" });
    if (this.claudeFound()) return h("span", { class: "provider-notes tone-good", text: "Found on this computer." });
    const spec = ai.data.providers.find((p) => p.id === CLAUDE_CODE);
    const link = safeUrl(spec?.key_url) || safeUrl(CLAUDE_CODE_URL);
    return h("span", { class: "provider-notes" }, "Not found. Install it from ",
      h("a", { class: "link", href: link, target: "_blank", rel: "noopener noreferrer", text: "claude.com/claude-code" }),
      " and sign in, then check again.");
  }

  aiStep() {
    this.loadAi();
    const ai = this.ai;
    const next = h("button", { type: "button", class: "btn btn-primary", onclick: () => this.leaveAi(next) });
    this.syncAiNext = () => {
      next.disabled = !this.aiReady();
      next.textContent = ai.mode === "other" && !ai.saved ? "Save and continue" : "Next: resume";
      next.title = !next.disabled ? ""
        : ai.mode === "claude" ? "Install Claude Code first, or pick another provider"
          : ai.mode === "other" ? "Test the provider first" : "Pick an AI provider";
    };
    if (ai.error) {
      this.syncAiNext();
      return h("div", { class: "setup-body" },
        h("p", { class: "field-error", role: "alert", text: "Couldn't load the list of AI providers. Try again." }),
        h("div", { class: "setup-actions" }, h("button", { type: "button", class: "btn", text: "Try again",
          onclick: () => {
            ai.error = null;
            this.render();
          } }), h("span", { class: "filter-spacer" }), next));
    }
    const choice = (mode, title, detail) => h("label", { class: `provider-card${ai.mode === mode ? " is-chosen" : ""}` },
      h("input", { type: "radio", name: "ai-mode", value: mode, checked: ai.mode === mode, disabled: !ai.data,
        onchange: () => {
          ai.mode = mode;
          this.render();
          this.card.querySelector(`input[value="${mode}"]`)?.focus();
        } }),
      h("span", { class: "provider-card-head" }, h("span", { class: "provider-name", text: title })),
      detail);
    const recheck = ai.mode === "claude" && ai.data && !this.claudeFound()
      && h("button", { type: "button", class: "btn btn-sm", onclick: () => {
        ai.data = null;
        this.render();
      } }, icon("refresh"), "Check again");
    this.syncAiNext();
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn uses an AI provider to read and rank jobs for you. Pick Claude Code if you have it, or choose another provider. Several have a free plan." }),
      h("div", { class: "provider-cards setup-ai", role: "radiogroup", "aria-label": "AI provider" },
        choice("claude", "Claude Code", this.claudeNote()),
        choice("other", "I don't have Claude Code", h("span", { class: "provider-notes", text: "Use Gemini, Groq, Mistral, OpenRouter, Anthropic, Ollama or another service." }))),
      recheck,
      ai.mode === "other" && ai.data && this.otherPicker(),
      h("div", { class: "setup-actions" }, h("span", { class: "filter-spacer" }), next));
  }

  /**
   * Make the choice the active provider and go on: Claude Code through PUT
   * /api/llm, another provider through the picker's Save, which takes only a
   * provider that answered a test.
   */
  async leaveAi(button) {
    button.disabled = true;
    const ai = this.ai;
    if (ai.mode === "claude" && ai.data.current.provider !== CLAUDE_CODE) {
      try {
        ai.data.current = await api("/api/llm", { method: "PUT", body: { provider: CLAUDE_CODE } });
      } catch {
        // api() has shown the error
        button.disabled = false;
        return;
      }
    } else if (ai.mode === "other" && !ai.saved) {
      ai.saved = await this.picker.save();
      if (!ai.saved) {
        this.syncAiNext();
        return;
      }
    }
    if (this.mounted) this.go(RESUME_STEP);
  }

  // step 4, preferences
  prefsStep() {
    const p = this.prefs;
    const field = (label, help, control, id) => h("div", { class: "field" },
      h(id ? "label" : "span", { class: "field-label", for: id, text: label }),
      help && h("p", { class: "field-help" }, help), control);
    const words = (key, id) => tagInput(p[key], { id, onChange: (tags) => { p[key] = tags; } });
    const lists = { title_keywords: words("title_keywords", "pref-title-keywords"),
      title_exclude_field: words("title_exclude_field", "pref-title-field") };
    const toggle = (role, on) => {
      Object.assign(p, withRole(p, this.options.roles, this.roles, role, on));
      this.roles = Object.keys(this.options.roles).filter((r) => (r === role ? on : this.roles.includes(r)));
      for (const key of Object.keys(lists)) {
        const fresh = words(key, lists[key].querySelector("input").id);
        lists[key].replaceWith(fresh);
        lists[key] = fresh;
      }
    };
    const roleNames = Object.keys(this.options.roles);
    const roles = roleNames.length > 0 && h("div", { class: "check-grid", role: "group", "aria-label": "Roles" },
      roleNames.map((role) => h("label", { class: "check" },
        h("input", { type: "checkbox", checked: this.roles.includes(role),
          onchange: (event) => toggle(role, event.target.checked) }), roleLabel(role))));
    const auth = h("select", { class: "select", id: "pref-auth", onchange: (event) => { p.work_authorization = event.target.value; } },
      this.options.work_authorization.map((value) => h("option", { value, selected: value === p.work_authorization,
        text: value === "unknown" ? "Prefer not to say" : value })));
    const next = h("button", { type: "button", class: "btn btn-primary", text: "Next: companies",
      disabled: Boolean(this.graduationError), onclick: () => this.go(COMPANIES_STEP) });
    const monthError = h("p", { class: "field-error", id: "pref-month-error", role: "alert",
      hidden: !this.graduationError, text: this.graduationError || "" });
    const month = h("input", { type: "month", class: "input input-month", id: "pref-month", min: "2000-01", max: "2100-12",
      placeholder: "YYYY-MM", value: this.graduation, "aria-describedby": "pref-month-error",
      "aria-invalid": String(Boolean(this.graduationError)),
      oninput: () => {
        const { year, error } = readGraduation(month);
        this.graduation = month.value;
        this.graduationError = error || null;
        if (!error) p.graduation_year = year;
        monthError.hidden = !error;
        monthError.textContent = error || "";
        month.setAttribute("aria-invalid", String(Boolean(error)));
        next.disabled = Boolean(error);
      } });
    const anchors = h("div", { class: "anchor-inputs" }, p.calibre_anchors.map((value, i) => h("input", {
      type: "text", class: "input input-text", value, placeholder: ANCHOR_HINTS[i], "aria-label": `Company tier example ${i + 1}`,
      oninput: (event) => { p.calibre_anchors[i] = event.target.value; } })));
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn filled these in from your resume. You can change them later in Settings." }),
      roles && field("Roles", "Checking a role adds its job titles to the lists below. Unchecking it takes them out.", roles),
      field("Title keywords", "Cairn shows only jobs whose title has one of these words. Leave it empty to use Cairn’s own list.",
        lists.title_keywords, "pref-title-keywords"),
      field("Skip titles with", "Cairn hides jobs whose title has any of these words, such as senior roles.",
        words("title_exclude", "pref-title-exclude"), "pref-title-exclude"),
      field("Skip field roles with", "Cairn hides jobs in fields your resume doesn’t point to, such as hardware.",
        lists.title_exclude_field, "pref-title-field"),
      field("Locations", "Leave empty to see jobs in every location.",
        h("div", { class: "field-stack" }, tagInput(p.locations, { id: "pref-locations", placeholder: "Seattle, New York, CA…",
          onChange: (tags) => { p.locations = tags; } }),
        switchControl("Remote is fine too", p.remote_ok, (on) => { p.remote_ok = on; }),
        switchControl("Only jobs in the US", p.us_only, (on) => { p.us_only = on; })), "pref-locations"),
      field("Work authorization", "Cairn weighs this when it ranks jobs.", auth, "pref-auth"),
      field("Graduation month", "Cairn flags jobs that start before you graduate.",
        h("div", { class: "field-stack" }, month, monthError), "pref-month"),
      field("Experience", "For full-time jobs. Cairn reads each job's description and hides the ones asking for more years. Leave the second box empty to show them all.",
        h("div", { class: "years-row" },
          "I have", yearsInput("pref-years", p.experience_years, "Years of full-time experience you have",
            (n) => { p.experience_years = n ?? 0; }),
          "years of full-time experience. Show jobs asking for up to",
          yearsInput("pref-max-years", p.max_years_required, "Most years a job can ask for",
            (n) => { p.max_years_required = n; }),
          "years."), "pref-years"),
      field("Degrees", "Cairn hides jobs that accept none of these degrees.",
        tagInput(p.degrees_held, { id: "pref-degrees", placeholder: "Bachelor's, Master's…", onChange: (tags) => { p.degrees_held = tags; } }), "pref-degrees"),
      field("Internship terms", "Terms you can intern in, such as “Fall 2026”. Leave empty to skip internships.",
        tagInput(p.internship_terms, { id: "pref-terms", onChange: (tags) => { p.internship_terms = tags; } }), "pref-terms"),
      field("Company tier examples", "Name up to three companies and the score out of 100 you'd give each. Cairn rates other companies against them.", anchors),
      this.actions(REVIEW_STEP, next));
  }

  /** The row under a step: Back to the step before, then its main button. */
  actions(back, main) {
    return h("div", { class: "setup-actions" },
      h("button", { type: "button", class: "btn btn-ghost", text: "Back", onclick: () => this.go(back) }),
      h("span", { class: "filter-spacer" }), main);
  }

  // step 5, companies to follow
  companiesStep() {
    const p = this.prefs;
    const lists = h("div", { class: "starter-list", role: "group", "aria-label": "Starter lists" },
      this.starters.map((list) => h("label", { class: "check starter", title: list.companies.join(", ") },
        h("input", { type: "checkbox", checked: p.starter_watchlists.includes(list.id), onchange: (event) => {
          p.starter_watchlists = this.starters.map((l) => l.id)
            .filter((id) => (id === list.id ? event.target.checked : p.starter_watchlists.includes(id)));
        } }),
        h("span", { class: "starter-text" },
          h("strong", { text: list.name }),
          h("span", { class: "muted", text: ` · ${list.companies.length} companies` }),
          h("span", { class: "starter-names", text: `${list.companies.slice(0, 4).join(", ")} and more` })))));
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn checks the careers pages of companies you follow every hour, so you hear about their new jobs first." }),
      h("div", { class: "field" },
        h("span", { class: "field-label", text: "Start with a list" }),
        h("p", { class: "field-help", text: "Pick any number. You can remove single companies later in Settings." }),
        lists),
      h("div", { class: "field" },
        h("label", { class: "field-label", for: "pref-watchlist", text: "Other companies" }),
        h("p", { class: "field-help", text: "Type a name or paste a link to their careers page." }),
        tagInput(p.watchlist, { id: "pref-watchlist", placeholder: "Stripe, jobs.lever.co/palantir…", onChange: (tags) => { p.watchlist = tags; } })),
      this.actions(PREFS_STEP, h("button", { type: "button", class: "btn btn-primary", text: "Next: alerts",
        onclick: () => this.go(ALERTS_STEP) })));
  }

  // step 6, phone alerts
  alertsStep() {
    const p = this.prefs;
    const status = h("p", { class: "field-help", role: "status" });
    const topic = h("input", { type: "text", class: "input input-text", id: "pref-ntfy", value: p.ntfy_topic,
      autocomplete: "off", spellcheck: "false", placeholder: "Leave empty for no alerts",
      oninput: () => {
        p.ntfy_topic = topic.value.trim();
        test.disabled = !p.ntfy_topic;
        status.textContent = "";
      } });
    const make = h("button", { type: "button", class: "btn", text: "Make one", onclick: () => {
      topic.value = `cairn-${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;
      topic.dispatchEvent(new Event("input"));
    } });
    const test = h("button", { type: "button", class: "btn", text: "Send a test", disabled: !p.ntfy_topic,
      onclick: async () => {
        test.disabled = true;
        status.textContent = "Sending…";
        try {
          await api("/api/notify/test", { method: "POST", quiet: true, body: { topic: p.ntfy_topic } });
          status.textContent = "Sent. It should reach your phone in a few seconds.";
        } catch (error) {
          status.textContent = error.status === 400
            ? "A topic can use only letters, numbers, - and _, up to 64 of them."
            : `Couldn't send it: ${error.message}`;
        }
        test.disabled = !p.ntfy_topic;
      } });
    const finish = h("button", { type: "button", class: "btn btn-primary", text: "Finish",
      disabled: !this.aiDone(), title: this.aiDone() ? null : "Set up the AI provider first",
      onclick: () => this.finish(finish) });
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn can send an alert to your phone when it finds a strong match. This is optional, and you can set it up later in Settings." }),
      h("ol", { class: "setup-howto" },
        h("li", { text: "Install the free ntfy app from the App Store or Google Play." }),
        h("li", { text: "Press Make one below. Anyone who knows this topic can read your alerts, so keep it long." }),
        h("li", { text: "In the ntfy app, tap + and subscribe to the same topic." }),
        h("li", { text: "Press Send a test and check your phone." })),
      h("div", { class: "field" },
        h("label", { class: "field-label", for: "pref-ntfy", text: "Topic" }),
        h("div", { class: "topic-row" }, topic, make, test),
        status),
      this.conflict && this.conflictBox(() => this.finish(finish)),
      this.error && h("p", { class: "field-error", role: "alert", text: this.error }),
      this.actions(COMPANIES_STEP, finish));
  }

  /**
   * The answers to send. A topic goes only when it was edited, so an empty field
   * clears a saved topic only when the user emptied it; the watchlist sends only
   * companies it did not already hold, since apply adds to it.
   */
  answers() {
    const { ntfy_topic: topic, watchlist, calibre_anchors: anchors, ...rest } = this.prefs;
    const prefs = { ...rest, calibre_anchors: anchors.map((a) => a.trim()).filter(Boolean), overwrite: this.overwrite };
    if (topic !== this.initial.ntfy_topic) prefs.ntfy_topic = topic;
    const added = watchlist.filter((name) => !this.initial.watchlist.includes(name));
    if (added.length) prefs.watchlist = added;
    return prefs;
  }

  async finish(button) {
    const prefs = this.answers();
    button.disabled = true;
    button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }),
      prefs.watchlist ? "Looking up companies…" : "Saving…");
    this.error = null;
    this.conflict = null;
    try {
      this.result = await api("/api/onboard/apply", { method: "POST", quiet: true,
        body: { profile_md: this.profile, prefs } });
      this.ctx.refreshStatus();
    } catch (error) {
      if (error.status === 409) this.conflict = this.conflictFiles(error);
      else this.error = `Couldn't save: ${error.message}`;
    }
    if (!this.mounted) return;
    this.render();
    this.card.querySelector("h1").focus({ preventScroll: true });
  }

  // finished
  finished() {
    const { unresolved } = this.result;
    const checks = h("div", { class: "setup-doctor" });
    const label = h("span", { text: "Find my jobs" });
    const find = h("button", { type: "button", class: "btn btn-primary" }, icon("search"), label);
    find.addEventListener("click", () => this.findJobs(find, label, checks));
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Your first run collects today's postings and ranks every one that matches your preferences. After that, each run ranks only new postings." }),
      unresolved.length > 0 && h("p", { class: "field-error", text: `Couldn't find a careers page for ${unresolved.join(", ")}. Add them in Settings › Sources by pasting a link.` }),
      h("div", { class: "setup-actions" }, find, h("span", { class: "filter-spacer" }),
        h("a", { href: "#today", class: "link", text: "Skip for now" })),
      checks);
  }

  /**
   * Check this Mac, stopping only for a failed required check, then start the first
   * run on its own screen.
   */
  async findJobs(button, label, checks) {
    button.disabled = true;
    label.textContent = "Checking this computer…";
    checks.replaceChildren();
    // a check that cannot run holds nothing back, since the run reports its own failures
    const found = await api("/api/doctor", { method: "POST", quiet: true }).then((answer) => answer.checks, () => []);
    if (!this.mounted) return;
    const broken = found.filter((check) => check.required && !check.ok);
    if (broken.length) {
      checks.replaceChildren(h("p", { class: "field-error", role: "alert",
        text: `The run needs ${broken.length === 1 ? "this" : "these"} fixed first:` }), doctorList(broken));
      button.disabled = false;
      label.textContent = "Check again";
      return;
    }
    this.followRun().start();
  }

  /** @returns {FirstRunScreen} the run screen, shown in place of the wizard */
  followRun() {
    this.runScreen = new FirstRunScreen(this.ctx);
    this.showScreen(this.runScreen.element);
    return this.runScreen;
  }
}

/**
 * The first-run setup wizard: AI provider, resume, review, preferences.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Setup(root, ctx).mount();
}
