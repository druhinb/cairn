import { api } from "../lib/api.js";
import { fmt, h, safeUrl } from "../lib/dom.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";

const EDITABLE_BASE_URL = new Set(["ollama", "custom"]);

/**
 * @typedef {object} Provider  an entry of GET /api/llm/providers
 * @property {string} id
 * @property {string} label
 * @property {string} base_url
 * @property {string} key_url
 * @property {{strong: string, cheap: string}} models
 * @property {string[]} steps
 * @property {boolean} free
 * @property {boolean} needs_key
 * @property {string} notes
 * @property {boolean} configured  for claude-code, whether the CLI was found
 */

/**
 * GET /api/llm/providers, /api/llm and, unless `keys` is false, /api/keys, for
 * providerPicker. Without /api/keys only the active provider knows it has a key.
 * @param {{keys?: boolean}} [options]
 */
export async function loadProviders({ keys = true } = {}) {
  const [list, current, stored] = await Promise.all([api("/api/llm/providers", { quiet: true }),
    api("/api/llm", { quiet: true }), keys ? api("/api/keys", { quiet: true }) : {}]);
  return { providers: list.providers, current, keys: stored };
}

/**
 * The AI provider picker: one radio card per provider, and for the chosen one its
 * setup steps, a key link, the key field, model fields, a base URL where the
 * provider takes one, Test and Save. Save writes through PUT /api/llm at once.
 * `only` limits the cards to those provider ids; `requireTest` holds Save back until
 * a Test answered on the values as they are; `onChange` runs when the provider, key,
 * a model or the base URL changes; `onTested` gets whether the provider answered on
 * the current values.
 * @param {{providers: Provider[], current: {provider: string, model: string, model_cheap: string, base_url: string, has_key: boolean}, keys: Record<string, boolean>}} data
 * @param {{only?: string[], requireTest?: boolean, onChange?: () => void, onPick?: (id: string) => void,
 *   onTested?: (ok: boolean, id: string) => void, onSaved?: (current: object) => void}} [options]
 * @returns {{element: HTMLElement, chosen: () => string, tested: () => boolean, save: () => Promise<boolean>}}
 */
export function providerPicker(data, { only, requireTest = false, onChange, onPick, onTested, onSaved } = {}) {
  let current = data.current;
  const keys = { ...data.keys };
  const providers = data.providers.filter((p) => !only || only.includes(p.id));
  let chosen = providers.some((p) => p.id === current.provider) ? current.provider : providers[0]?.id;
  /** @type {Record<string, {key: string | null, model: string, model_cheap: string, base_url: string}>} */
  const drafts = {};
  const draftOf = (id) => {
    drafts[id] ??= id === current.provider
      ? { key: "", model: current.model || "", model_cheap: current.model_cheap || "", base_url: current.base_url || "" }
      : { key: "", model: "", model_cheap: "", base_url: "" };
    return drafts[id];
  };
  const hasKey = (id) => (id === current.provider ? current.has_key : Boolean(keys[id]));
  const cards = h("div", { class: "provider-cards", role: "radiogroup", "aria-label": "AI provider" });
  const panel = h("div", { class: "provider-panel", "aria-live": "polite" });
  const element = h("div", { class: "provider-picker" }, cards, panel);
  /** bumped on every change to the values; a Test counts for the version it ran on */
  let version = 0;
  let testedVersion = -1;
  let saveButton = null;
  const tested = () => testedVersion === version;
  const syncSave = () => {
    if (!saveButton || !requireTest) return;
    saveButton.disabled = !tested();
    saveButton.title = tested() ? "" : "Press Test first. Save turns on once the test works";
  };
  const changed = () => {
    version += 1;
    if (result?.textContent) {
      result.className = "provider-result";
      result.textContent = "Changed since the last test. Test again.";
    }
    syncSave();
    onChange?.();
  };
  element.addEventListener("input", changed);

  const drawCards = () => {
    cards.replaceChildren(...providers.map((p) => h("label", { class: `provider-card${p.id === chosen ? " is-chosen" : ""}` },
      h("input", { type: "radio", name: "llm-provider", value: p.id, checked: p.id === chosen,
        onchange: () => pick(p.id) }),
      h("span", { class: "provider-card-head" }, h("span", { class: "provider-name", text: p.label }),
        p.free && h("span", { class: "tag tag-free", text: "Free" }),
        p.id === current.provider && h("span", { class: "tag tag-active", text: "In use" })),
      h("span", { class: "provider-notes", text: p.notes }))));
  };

  const pick = (id) => {
    chosen = id;
    for (const card of cards.children) card.classList.toggle("is-chosen", card.querySelector("input").value === id);
    drawPanel();
    onPick?.(id);
  };

  const body = (spec, draft) => {
    const out = { provider: spec.id, model: draft.model.trim(), model_cheap: draft.model_cheap.trim() };
    if (EDITABLE_BASE_URL.has(spec.id)) out.base_url = draft.base_url.trim();
    return out;
  };

  let result = null;
  const drawPanel = () => {
    const spec = providers.find((p) => p.id === chosen);
    if (!spec) {
      panel.replaceChildren();
      return;
    }
    const draft = draftOf(spec.id);
    const field = (label, control, help) => h("label", { class: "field provider-field" },
      h("span", { class: "field-label", text: label }), help && h("span", { class: "field-help", text: help }), control);
    const text = (name, placeholder, extra = {}) => h("input", { type: "text", class: "input input-text",
      value: draft[name] || "", placeholder, spellcheck: "false", autocomplete: "off", ...extra,
      oninput: (event) => { draft[name] = event.target.value; } });
    const keyLink = safeUrl(spec.key_url);
    const stored = hasKey(spec.id);
    const keyInput = h("input", { type: "password", class: "input input-text", autocomplete: "new-password",
      "data-1p-ignore": true, "data-lpignore": "true", spellcheck: "false",
      placeholder: draft.key === null ? "Removed when you save" : stored ? "Paste a new key to replace yours"
        : "Paste your key",
      value: draft.key || "", oninput: (event) => { draft.key = event.target.value; } });
    const clear = h("button", { type: "button", class: "btn btn-sm", text: "Clear", disabled: !stored || draft.key === null,
      title: "Remove your key when you save", onclick: () => {
        draft.key = null;
        drawPanel();
        changed();
      } });
    const test = h("button", { type: "button", class: "btn btn-sm" }, icon("play"), "Test");
    const save = h("button", { type: "button", class: "btn btn-primary btn-sm", text: "Save" });
    saveButton = save;
    syncSave();
    result = h("p", { class: "provider-result", role: "status" });
    test.addEventListener("click", () => runTest(spec, draft, test));
    save.addEventListener("click", () => saveProvider(spec, draft, save));
    panel.replaceChildren(...[
      h("h3", { class: "field-label", text: `Set up ${spec.label}` }),
      h("ol", { class: "provider-steps" }, spec.steps.map((step) => h("li", { text: step }))),
      keyLink && h("a", { class: "link provider-key-link", href: keyLink, target: "_blank", rel: "noopener noreferrer" },
        spec.needs_key ? "Get a key" : `Get ${spec.label}`, icon("external")),
      (spec.needs_key || spec.id === "custom") && field("API key",
        h("div", { class: "provider-key" }, keyInput, clear)),
      h("div", { class: "provider-models" },
        field("Ranking model", text("model", spec.models.strong || "model id")),
        field("Summary model", text("model_cheap", spec.models.cheap || "model id"))),
      EDITABLE_BASE_URL.has(spec.id) && field("Address", text("base_url", spec.base_url || "https://…/v1"),
        "The web address the service gives you."),
      h("div", { class: "section-actions" }, test, save),
      result].filter(Boolean));
  };

  const runTest = async (spec, draft, button) => {
    button.disabled = true;
    result.className = "provider-result";
    result.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }), ` Asking ${spec.label}…`);
    const request = body(spec, draft);
    if (draft.key) request.key = draft.key;
    delete request.model_cheap;
    const ran = version;
    let ok = false;
    try {
      const answer = await api("/api/llm/test", { method: "POST", body: request, quiet: true });
      ok = answer.ok;
      result.className = `provider-result ${ok ? "tone-good" : "tone-weak"}`;
      result.textContent = ok ? `It works. ${spec.label} answered in ${fmt.duration(answer.latency_ms / 1000)}.` : `It didn't work: ${answer.error}`;
    } catch (error) {
      result.className = "provider-result tone-weak";
      result.textContent = `Couldn't run the test: ${error.message}`;
    } finally {
      button.disabled = false;
    }
    if (ok && ran === version) testedVersion = ran;
    syncSave();
    onTested?.(ok && ran === version, spec.id);
  };

  const saveProvider = async (spec, draft, button) => {
    if (requireTest && !tested()) return false;
    if (button) button.disabled = true;
    const request = { ...body(spec, draft), key: draft.key === null ? null : draft.key || "" };
    try {
      current = await api("/api/llm", { method: "PUT", body: request, quiet: true });
      keys[spec.id] = current.has_key;
      Object.keys(drafts).forEach((id) => delete drafts[id]);
      toast(`Saved. Cairn now uses ${spec.label}`);
      drawCards();
      drawPanel();
      onSaved?.(current);
      return true;
    } catch (error) {
      toast(`Couldn't save the provider: ${error.message}`, { tone: "error" });
      if (button) button.disabled = false;
      return false;
    }
  };

  drawCards();
  drawPanel();
  return {
    element,
    chosen: () => chosen,
    tested,
    save: () => {
      const spec = providers.find((p) => p.id === chosen);
      return spec ? saveProvider(spec, draftOf(spec.id), null) : Promise.resolve(false);
    },
  };
}
