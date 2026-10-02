import { api } from "../lib/api.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { readNumber } from "../lib/numbers.js";
import { QUIZ_CARDS, quizCards, statedAuthorization } from "../lib/quiz.js";
import { heldRoles, heldSkips, withRole, withSkip } from "../lib/roles.js";
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
import { JOB_TYPES, SECTIONS } from "./settingsFields.js";

const STEPS = ["AI", "Resume", "Review", "Preferences", "Companies", "Alerts"];
const [AI_STEP, RESUME_STEP, REVIEW_STEP, PREFS_STEP, COMPANIES_STEP, ALERTS_STEP] = STEPS.keys();
const CLAUDE_CODE = "claude-code";
const CLAUDE_CODE_URL = "https://claude.com/claude-code";
const ROLE_LABELS = { backend: "Backend", frontend: "Frontend", fullstack: "Full stack", systems: "Systems",
  ml: "Machine learning", data: "Data", quant: "Quant", research: "Research", platform: "Platform",
  security: "Security", mobile: "Mobile", embedded: "Embedded" };
const SKIP_LABELS = { senior: "Senior roles", managers: "Managers", phd: "PhD roles", hardware: "Hardware",
  engineering: "Other engineering", testing: "Test and quality", support: "Field and support",
  business: "Sales and recruiting" };
const CHANGE_LISTS = { roles: withRole, skips: withSkip };
const JOB_TYPE_NOTES = { internships: "Summer, fall or co-op roles while you're still in school.",
  new_grad: "Full-time roles for when you finish school.", both: "Internships and full-time roles together." };
const ANCHOR_HINTS = ["e.g. Stripe, Databricks, Jane Street = 90", "e.g. Datadog, Snowflake = 75",
  "e.g. a strong regional company = 60"];
const ACCEPT = ".pdf,.txt,.md";
const AI_LIMIT_KEYS = ["model_concurrency", "rank_batch_size", "rank_retries", "max_rank_per_run",
  "max_summaries_per_run", "monthly_call_cap", "fetch_descriptions"];
const SETTING_FIELDS = SECTIONS.flatMap((section) => section.fields || []);
const AI_LIMITS = AI_LIMIT_KEYS.map((key) => SETTING_FIELDS.find((spec) => spec.key === key));
// what /api/onboard/options answers, for when it cannot be reached; without the
// role keywords and skip groups there are no checkboxes
const BUILT_IN_OPTIONS = { roles: {}, skips: {}, work_authorization: ["US citizen", "F-1 OPT", "needs sponsorship", "unknown"] };
const GRADUATION_MONTH = /^(\d{4})-(0[1-9]|1[0-2])$/;
const DEFAULT_GRADUATION_MONTH = "06";
const DEFAULT_MAX_YEARS = 2;
const LOOKBACK = { min: 1, max: 365 };
const DEFAULT_LOOKBACK_DAYS = 90;
const READ_LINE = /^\[setup\] read (\d+) words/;
const SLOW_DRAFT_SECONDS = 60;
// long enough to see the draft pass its check before Review replaces the screen
const CHECKED_PAUSE_MS = 700;

const labelOf = (labels, name) => labels[name] || name[0].toUpperCase() + name.slice(1);

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

/** A labelled field; without an id the label is plain text. */
function field(label, help, control, id) {
  return h("div", { class: "field" },
    h(id ? "label" : "span", { class: "field-label", for: id, text: label }),
    help && h("p", { class: "field-help" }, help), control);
}

function sizeText(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

class Setup {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.step = 0;
    /** whether only the preference questions are asked again, from Settings */
    this.retake = ctx.params.get("retake") === "1";
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
    /** the role and skip group checkboxes checked on the preferences step */
    this.checked = { roles: [], skips: [] };
    /** the preference card shown, whether the advanced card is on, and the way the last move went */
    this.quiz = { card: QUIZ_CARDS[0], advanced: false, moved: null };
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
    /**
     * The AI limits from /api/settings: values holds the edits, saved what config.toml
     * holds. Null hides them, as when the settings can't be read.
     */
    this.limits = null;
    this.limitsOpen = false;
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
    if (this.retake) {
      await this.openRetake();
      return;
    }
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

  /**
   * Ask the preference questions alone, starting from the current settings and the
   * work authorization profile.md states; Save sends them without a profile, so
   * apply keeps profile.md and writes the answers into it.
   */
  async openRetake() {
    let current, profile, status;
    try {
      [current, profile, status] = await Promise.all([api("/api/settings", { quiet: true }),
        api("/api/files/profile", { quiet: true }), api("/api/onboard/status", { quiet: true }), this.loadOptions()]);
    } catch (error) {
      this.error = `Couldn't load your settings: ${error.message}`;
    }
    if (!this.mounted) return;
    this.showCard();
    if (status?.needs_setup) this.error = "Cairn has no profile of yours yet, so run setup first.";
    if (this.error) {
      this.card.replaceChildren(h("p", { class: "field-error", role: "alert", text: this.error }),
        h("div", { class: "setup-actions" }, h("a", { href: "#settings", class: "link", text: "Back to Settings" }),
          h("span", { class: "filter-spacer" }), h("a", { href: "#setup", class: "btn btn-primary", text: "Run setup" })));
      return;
    }
    this.prefs = this.startingPrefs({}, current);
    const roles = heldRoles(this.prefs, this.options);
    this.checked = { roles, skips: heldSkips(this.prefs, this.options, roles) };
    this.prefs.work_authorization = statedAuthorization(profile.text, this.options.work_authorization);
    this.initial.work_authorization = this.prefs.work_authorization;
    this.step = PREFS_STEP;
    this.render();
    this.card.querySelector("h1").focus({ preventScroll: true });
  }

  /** Save the answers given again, then go back to Settings. */
  async saveAnswers() {
    const button = this.card.querySelector(".setup-actions .btn-primary");
    button.disabled = true;
    button.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), "Saving…");
    try {
      await api("/api/onboard/apply", { method: "POST", quiet: true, body: { prefs: this.answers() } });
    } catch (error) {
      toast(`Couldn't save: ${error.message}`, { tone: "error" });
      if (this.mounted) this.render();
      return;
    }
    toast("Saved your answers");
    this.ctx.refreshStatus();
    this.ctx.navigate("settings", new URLSearchParams({ section: "preferences" }));
  }

  render() {
    if (this.retake) {
      this.card.replaceChildren(
        h("header", { class: "setup-head" }, pixelArt("checklist"),
          h("h1", { class: "display", tabindex: "-1", text: "Your preferences" }),
          h("p", { class: "setup-sub", text: "Answer the setup questions again. Cairn keeps your profile and changes only what these answers cover." })),
        this.prefsStep());
      return;
    }
    const done = this.result != null;
    const body = done ? this.finished()
      : [this.aiStep, this.resumeStep, this.reviewStep, this.prefsStep, this.companiesStep, this.alertsStep][this.step].call(this);
    this.card.replaceChildren(
      h("header", { class: "setup-head" }, pixelArt("checklist"),
        h("h1", { class: "display", tabindex: "-1", text: done ? "You are set up" : "Set up Cairn" }),
        h("p", { class: "setup-sub", text: done ? "Setup saved your profile and preferences."
          : "Pick an AI provider and add your resume. Cairn drafts a profile from it for you to check, then you answer a few quick questions." })),
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
    const places = allow.filter((place) => !isRemote(place));
    const watchlist = (current?.watchlist || []).map((spec) => spec.company || spec.location);
    const year = suggestions.graduation_year ?? current?.graduation_year ?? null;
    const month = suggestions.graduation_year != null && suggestions.graduation_month
      ? String(suggestions.graduation_month).padStart(2, "0") : DEFAULT_GRADUATION_MONTH;
    this.graduation = year == null ? "" : `${year}-${month}`;
    this.initial = { ntfy_topic: current?.notify_ntfy_topic || "", watchlist };
    const prefs = {
      title_keywords: suggestions.title_keywords ?? current?.title_keywords ?? [],
      title_exclude: suggestions.title_exclude ?? current?.title_exclude ?? [],
      title_exclude_field: suggestions.title_exclude_field ?? current?.title_exclude_field ?? [],
      // a list of Remote alone stays whole, since no places at all reads as everywhere
      locations: either(suggestions.locations, places.length ? places : allow),
      remote_ok: allow.some(isRemote),
      us_only: current?.us_only ?? false,
      job_type: suggestions.job_type ?? current?.job_type ?? "both",
      graduation_year: year,
      experience_years: current?.experience_years ?? 0,
      max_years_required: current ? current.max_years_required : DEFAULT_MAX_YEARS,
      degrees_held: either(suggestions.degrees_held, current?.degrees_held),
      internship_terms: either(suggestions.internship_terms, current?.wanted_intern_terms),
      recent_days: current?.recent_days ?? DEFAULT_LOOKBACK_DAYS,
      work_authorization: suggestions.work_authorization || "unknown",
      calibre_anchors: ["", "", ""],
      ntfy_topic: this.initial.ntfy_topic,
      watchlist: [...watchlist],
      starter_watchlists: [],
    };
    const roles = suggestions.roles || [];
    this.checked = { roles, skips: heldSkips(prefs, this.options, roles) };
    return prefs;
  }

  // step 3, review profile.md
  reviewStep() {
    const edit = editor(this.profile, { label: "Profile draft", onInput: (text) => { this.profile = text; } });
    return h("div", { class: "setup-body" },
      h("p", { class: "setup-lead", text: "Cairn compares every job to this profile. Fix anything it got wrong. Next come a few quick questions about the jobs you want." }),
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
      let current;
      [ai.data, current] = await Promise.all([loadProviders({ keys: false }),
        this.limits ? null : api("/api/settings", { quiet: true }).catch(() => null)]);
      if (current) {
        const saved = Object.fromEntries(AI_LIMIT_KEYS.map((key) => [key, current[key]]));
        this.limits = { saved, values: { ...saved } };
      }
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
    const badLimits = new Set();
    this.syncAiNext = () => {
      next.disabled = !this.aiReady() || badLimits.size > 0;
      next.textContent = ai.mode === "other" && !ai.saved ? "Save and continue" : "Next: resume";
      next.title = !next.disabled ? ""
        : badLimits.size > 0 ? "Fix the AI limits first"
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
      this.limits && this.limitsSection(badLimits),
      h("div", { class: "setup-actions" }, h("span", { class: "filter-spacer" }), next));
  }

  /** The AI limits, folded away; `bad` collects the keys whose box holds no usable number. */
  limitsSection(bad) {
    const { values } = this.limits;
    const label = (spec, id) => h("label", { class: "field-label", for: id, text: spec.label });
    const help = (spec, id) => h("p", { class: "field-help", id: `${id}-help`, text: spec.help });
    const numberField = (spec) => {
      const id = `ai-${spec.key}`;
      const error = h("p", { class: "field-error", id: `${id}-error`, role: "alert", hidden: true });
      const input = h("input", { type: "number", class: "input input-num", id, min: spec.min, max: spec.max, step: "1",
        value: values[spec.key] ?? "", placeholder: spec.nullable ? "none" : null,
        "aria-describedby": `${id}-help ${id}-error`,
        oninput: () => {
          const { value, error: problem } = readNumber(input, spec);
          if (problem) bad.add(spec.key);
          else {
            bad.delete(spec.key);
            values[spec.key] = value;
          }
          error.hidden = !problem;
          error.textContent = problem || "";
          input.setAttribute("aria-invalid", String(Boolean(problem)));
          this.syncAiNext();
        } });
      return h("div", { class: "field field-number" }, label(spec, id), help(spec, id),
        h("div", { class: "number-input" }, input, spec.unit && h("span", { class: "unit", text: spec.unit })), error);
    };
    const switchField = (spec) => {
      const id = `ai-${spec.key}`;
      const control = switchControl(null, values[spec.key], (on) => { values[spec.key] = on; }, { id, key: spec.key });
      control.querySelector("input").setAttribute("aria-describedby", `${id}-help`);
      return h("div", { class: "field field-switch" }, label(spec, id), help(spec, id), control);
    };
    return h("details", { class: "details setup-limits", open: this.limitsOpen,
      ontoggle: (event) => { this.limitsOpen = event.target.open; } },
      h("summary", { text: "AI limits" }),
      h("div", { class: "setup-limits-body" },
        h("p", { class: "field-help", text: "Cairn starts with limits that suit most providers. Lower them if your provider says you sent too many requests. You can change them later in Settings › Ranking." }),
        AI_LIMITS.map((spec) => (spec.type === "switch" ? switchField(spec) : numberField(spec)))));
  }

  /** Save the AI limits that differ from config.toml; false when the save fails. */
  async saveLimits() {
    if (!this.limits) return true;
    const { saved, values } = this.limits;
    const changed = Object.fromEntries(AI_LIMIT_KEYS.filter((key) => values[key] !== saved[key])
      .map((key) => [key, values[key]]));
    if (Object.keys(changed).length === 0) return true;
    try {
      await api("/api/settings", { method: "PUT", body: changed });
    } catch {
      // api() has shown the error
      return false;
    }
    Object.assign(saved, changed);
    return true;
  }

  /**
   * Make the choice the active provider and go on: Claude Code through PUT
   * /api/llm, another provider through the picker's Save, which takes only a
   * provider that answered a test. The AI limits save after it.
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
    if (!(await this.saveLimits())) {
      button.disabled = false;
      return;
    }
    if (this.mounted) this.go(RESUME_STEP);
  }

  // step 4, preferences, one question to a card
  /** The preference cards to ask, given the answers so far. */
  quizCards() {
    return quizCards({ jobType: this.prefs.job_type, roles: Object.keys(this.options.roles).length > 0,
      advanced: this.quiz.advanced });
  }

  /**
   * Show the card `step` cards away, or leave the questions past either end. The
   * card stays put when they are left, so Back from Companies returns to the last.
   */
  moveQuiz(step) {
    const cards = this.quizCards();
    const at = cards.indexOf(this.quiz.card) + step;
    if (at < 0) return this.retake ? this.ctx.navigate("settings") : this.go(REVIEW_STEP);
    if (at >= cards.length) return this.retake ? this.saveAnswers() : this.go(COMPANIES_STEP);
    this.quiz.card = cards[at];
    this.quiz.moved = step > 0 ? "next" : "back";
    this.render();
    this.card.querySelector(".quiz-question").focus({ preventScroll: true });
    this.card.closest(".setup-scroll").scrollTop = 0;
  }

  prefsStep() {
    const cards = this.quizCards();
    if (!cards.includes(this.quiz.card)) this.quiz.card = cards.at(-1);
    const at = cards.indexOf(this.quiz.card);
    const last = this.retake ? "Save" : "Next: companies";
    const next = h("button", { type: "button", class: "btn btn-primary",
      text: at === cards.length - 1 ? last : "Next", onclick: () => this.moveQuiz(1) });
    const sync = () => {
      const problem = card.problem?.() || null;
      next.disabled = Boolean(problem);
      next.title = problem || "";
    };
    const card = this[`${this.quiz.card}Card`](sync);
    sync();
    const moved = this.quiz.moved;
    this.quiz.moved = null;
    const advanced = switchControl("Advanced options", this.quiz.advanced, (on) => {
      this.quiz.advanced = on;
      this.render();
      this.card.querySelector('[data-key="quiz-advanced"]').focus();
    }, { key: "quiz-advanced" });
    return h("div", { class: "setup-body quiz" },
      h("div", { class: "quiz-bar" },
        h("span", { class: "quiz-count", text: `${at + 1} of ${cards.length}` }),
        h("span", { class: "quiz-meter", "aria-hidden": "true" },
          h("span", { class: "quiz-meter-fill", style: `width: ${((at + 1) / cards.length) * 100}%` })),
        advanced),
      h("div", { class: `quiz-card${moved ? ` quiz-${moved}` : ""}`, role: "group", "aria-labelledby": "quiz-question",
        onkeydown: (event) => {
          if (event.key !== "Enter" || event.defaultPrevented || event.isComposing || next.disabled
            || /^(BUTTON|A|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
          event.preventDefault();
          this.moveQuiz(1);
        } },
      h("h2", { class: "quiz-question", id: "quiz-question", tabindex: "-1", text: card.question }),
      card.help && h("p", { class: "setup-lead", text: card.help }),
      card.body),
      h("div", { class: "setup-actions" },
        h("button", { type: "button", class: "btn btn-ghost", text: this.retake && at === 0 ? "Cancel" : "Back",
          onclick: () => this.moveQuiz(-1) }),
        h("span", { class: "filter-spacer" }), next));
  }

  /** Check or uncheck a role or skip group, and change the title lists to match. */
  toggleCheck(kind, name, on) {
    Object.assign(this.prefs, CHANGE_LISTS[kind](this.prefs, this.options, this.checked, name, on));
    this.checked[kind] = Object.keys(this.options[kind])
      .filter((other) => (other === name ? on : this.checked[kind].includes(other)));
  }

  /** The role or skip group checkboxes; `onToggle` runs after each change. */
  checks(kind, labels, groupLabel, onToggle) {
    return h("div", { class: "check-grid", role: "group", "aria-label": groupLabel },
      Object.keys(this.options[kind]).map((name) => h("label", { class: "check" },
        h("input", { type: "checkbox", checked: this.checked[kind].includes(name),
          onchange: (event) => {
            this.toggleCheck(kind, name, event.target.checked);
            onToggle?.();
          } }), labelOf(labels, name))));
  }

  jobsCard() {
    const p = this.prefs;
    const choice = ([value, label]) => h("label", { class: `provider-card choice-card${p.job_type === value ? " is-chosen" : ""}` },
      h("input", { type: "radio", name: "job-type", value, checked: p.job_type === value, onchange: () => {
        p.job_type = value;
        this.render();
        this.card.querySelector(`input[name="job-type"][value="${value}"]`).focus();
      } }),
      h("span", { class: "provider-name", text: label }),
      h("span", { class: "provider-notes", text: JOB_TYPE_NOTES[value] }));
    return { question: "What kind of jobs are you looking for?",
      help: this.draft?.suggestions.graduation_year != null ? "Cairn picked one from your graduation date. You can change it." : null,
      body: h("div", { class: "choice-cards", role: "radiogroup", "aria-label": "Jobs to show" }, JOB_TYPES.map(choice)) };
  }

  rolesCard() {
    return { question: "Which roles interest you?",
      help: `Cairn checked the ones ${this.retake ? "your title keywords cover" : "your resume points to"}. Pick as many as you like.`,
      body: this.checks("roles", ROLE_LABELS, "Roles") };
  }

  placesCard() {
    const p = this.prefs;
    return { question: "Where do you want to work?",
      help: "Add cities, states or countries. Leave it empty to see jobs everywhere.",
      body: h("div", { class: "field-stack" },
        h("label", { class: "visually-hidden", for: "pref-locations", text: "Locations" }),
        tagInput(p.locations, { id: "pref-locations", placeholder: "Seattle, New York, CA…",
          onChange: (tags) => { p.locations = tags; } }),
        switchControl("Remote is fine too", p.remote_ok, (on) => { p.remote_ok = on; }),
        switchControl("Only jobs in the US", p.us_only, (on) => { p.us_only = on; })) };
  }

  schoolCard(sync) {
    const p = this.prefs;
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
        sync();
      } });
    const auth = h("select", { class: "select", id: "pref-auth", onchange: (event) => { p.work_authorization = event.target.value; } },
      this.options.work_authorization.map((value) => h("option", { value, selected: value === p.work_authorization,
        text: value === "unknown" ? "Prefer not to say" : value })));
    return { question: "When do you graduate?",
      body: h("div", { class: "field-stack" },
        field("Graduation month", "Cairn flags jobs that start before you graduate. Leave it empty to skip the flag.",
          h("div", { class: "field-stack" }, month, monthError), "pref-month"),
        field("Work authorization", "Cairn weighs this when it ranks jobs.", auth, "pref-auth")),
      problem: () => this.graduationError };
  }

  experienceCard() {
    const p = this.prefs;
    return { question: "How much full-time experience do you have?",
      help: "Don't count internships. Cairn hides full-time jobs that ask for more years than your limit. Leave the second box empty to show them all.",
      body: h("div", { class: "years-row" },
        "I have", yearsInput("pref-years", p.experience_years, "Years of full-time experience you have",
          (n) => { p.experience_years = n ?? 0; }),
        "years of full-time experience. Show jobs asking for up to",
        yearsInput("pref-max-years", p.max_years_required, "Most years a job can ask for",
          (n) => { p.max_years_required = n; }),
        "years.") };
  }

  termsCard(sync) {
    const p = this.prefs;
    const hint = h("p", { class: "field-help", role: "status" });
    const show = () => {
      hint.textContent = p.internship_terms.length ? "" : "Add at least one term to see internships.";
    };
    show();
    return { question: "Which internship terms can you take?",
      help: "Cairn shows internships only for the terms you list, such as Summer 2027 or Fall 2027.",
      body: h("div", { class: "field-stack" },
        h("label", { class: "visually-hidden", for: "pref-terms", text: "Internship terms" }),
        tagInput(p.internship_terms, { id: "pref-terms", placeholder: "Summer 2027, Fall 2027…", onChange: (tags) => {
          p.internship_terms = tags;
          show();
          sync();
        } }),
        hint),
      problem: () => (p.internship_terms.length ? null : "Add at least one term") };
  }

  recentCard(sync) {
    const p = this.prefs;
    let problem = null;
    const error = h("p", { class: "field-error", id: "pref-recent-error", role: "alert", hidden: true });
    const lookback = h("input", { type: "number", class: "input input-num", id: "pref-recent", min: LOOKBACK.min,
      max: LOOKBACK.max, step: "1", value: p.recent_days, "aria-describedby": "pref-recent-error",
      oninput: () => {
        const { value, error: bad } = readNumber(lookback, LOOKBACK);
        if (!bad) p.recent_days = value;
        problem = bad || null;
        error.hidden = !bad;
        error.textContent = bad || "";
        lookback.setAttribute("aria-invalid", String(Boolean(bad)));
        sync();
      } });
    return { question: "How far back should Cairn look?",
      help: "New grad and intern jobs often open in July and stay open for months.",
      body: h("div", { class: "field-stack" },
        h("div", { class: "years-row" }, h("label", { for: "pref-recent", text: "Show jobs posted in the last" }),
          lookback, "days"),
        error),
      problem: () => problem };
  }

  tiersCard() {
    const p = this.prefs;
    return { question: "Which companies do you rate highly?",
      help: "This one is optional. Name up to three companies and the score out of 100 you'd give each. Cairn rates other companies against them.",
      body: h("div", { class: "anchor-inputs" }, p.calibre_anchors.map((value, i) => h("input", {
        type: "text", class: "input input-text", value, placeholder: ANCHOR_HINTS[i], "aria-label": `Company tier example ${i + 1}`,
        oninput: (event) => { p.calibre_anchors[i] = event.target.value; } }))) };
  }

  advancedCard() {
    const p = this.prefs;
    const words = (key, id) => tagInput(p[key], { id, onChange: (tags) => { p[key] = tags; } });
    const lists = { title_keywords: words("title_keywords", "pref-title-keywords"),
      title_exclude: words("title_exclude", "pref-title-exclude"),
      title_exclude_field: words("title_exclude_field", "pref-title-field") };
    const refresh = () => {
      for (const key of Object.keys(lists)) {
        const fresh = words(key, lists[key].querySelector("input").id);
        lists[key].replaceWith(fresh);
        lists[key] = fresh;
      }
    };
    const skips = Object.keys(this.options.skips).length > 0 && this.checks("skips", SKIP_LABELS, "Jobs to skip", refresh);
    return { question: "Fine-tune the filters",
      help: "Cairn matches job titles against these lists. The roles you picked already filled them in.",
      body: h("div", { class: "setup-body" },
        field("Title keywords", "Cairn shows only jobs whose title has one of these words. Leave it empty to use Cairn’s own list.",
          lists.title_keywords, "pref-title-keywords"),
        skips && field("Jobs to skip", "Checking a group adds its words to the two skip lists below. Unchecking it takes them out.", skips),
        field("Skip titles with", "Cairn hides jobs whose title has any of these words, such as senior roles.",
          lists.title_exclude, "pref-title-exclude"),
        field("Skip field roles with", "Cairn hides jobs in fields your resume doesn’t point to, such as hardware.",
          lists.title_exclude_field, "pref-title-field"),
        field("Degrees", "Cairn hides jobs that accept none of these degrees.",
          tagInput(p.degrees_held, { id: "pref-degrees", placeholder: "Bachelor's, Master's…", onChange: (tags) => { p.degrees_held = tags; } }),
          "pref-degrees")) };
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
   * companies it did not already hold, since apply adds to it. A retake sends the
   * work authorization only when it changed, as profile.md already states it.
   */
  answers() {
    const { ntfy_topic: topic, watchlist, calibre_anchors: anchors, ...rest } = this.prefs;
    const prefs = { ...rest, calibre_anchors: anchors.map((a) => a.trim()).filter(Boolean), overwrite: this.overwrite };
    if (topic !== this.initial.ntfy_topic) prefs.ntfy_topic = topic;
    if (prefs.work_authorization === this.initial.work_authorization) delete prefs.work_authorization;
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
