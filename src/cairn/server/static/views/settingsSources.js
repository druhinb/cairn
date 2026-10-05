import { api } from "../lib/api.js";
import { fmt, h } from "../lib/dom.js";
import { followCompany, followedAt } from "../lib/follow.js";
import { kindLabel, sourceChip, specName } from "../components/chip.js";
import { icon } from "../components/icons.js";
import { switchControl } from "../components/switch.js";
import { toast } from "../components/toast.js";
import { SECTIONS, USAJOBS_EMAIL } from "./settingsFields.js";

export const KIND_HINTS = {
  github: "listings.json URL on raw.githubusercontent.com",
  greenhouse: "board slug, e.g. stripe",
  lever: "board slug, e.g. palantir",
  ashby: "board slug, e.g. ramp",
  smartrecruiters: "company id, e.g. Visa",
  workable: "account slug, e.g. huggingface",
  bamboohr: "subdomain, e.g. bamboohr",
  workday: "<tenant>.<wdN>/<site>, e.g. acme.wd5/Careers",
  github_readme: "raw README URL, e.g. https://raw.githubusercontent.com/owner/repo/main/README.md",
  hn_hiring: "whoishiring for the newest thread, or a thread URL",
  yc_waas: "YC jobs URL, e.g. https://www.ycombinator.com/jobs/role/software-engineer",
  remoteok: "https://remoteok.com/api",
  usajobs: "search keyword, e.g. software engineer",
  page: "careers page URL, e.g. https://example.com/careers",
};
const WORKDAY_HELP = "A Workday board has no short name to guess. Copy <tenant>.<wdN>/<site> from the board URL, "
  + "https://<tenant>.<wdN>.myworkdayjobs.com/<site>, or add the company below by that URL.";

const clone = (value) => JSON.parse(JSON.stringify(value));

export function renderSources(view) {
  const section = SECTIONS.find((s) => s.id === "sources");
  const errorFor = (key) => {
    const error = h("p", { class: "field-error", role: "alert", hidden: true });
    const wrap = h("div", { class: "field", "data-field": key });
    view.fields.set(key, { wrap, error });
    return { wrap, error };
  };
  const feeds = errorFor("sources");
  feeds.wrap.append(
    h("h3", { class: "field-label", text: "Feeds and boards" }),
    h("p", { class: "field-help", text: "The job sites Cairn checks on every run." }),
    specList(view, "sources", (spec) => [spec.company && h("span", { class: "spec-company", text: spec.company }),
      h("span", { class: "spec-name mono", title: spec.location, text: spec.location })]),
    addSourceForm(view), feeds.error);
  const watch = errorFor("watchlist");
  const lists = h("div", { class: "starter-lists" });
  const own = h("div", { class: "own-companies" });
  watch.wrap.append(
    h("h3", { class: "field-label", text: "Watchlist" }),
    h("p", { class: "field-help", text: "Companies you follow. Cairn checks their careers pages every hour, and tells you when a strong match appears." }),
    lists, own, addCompanyForm(view), sharingRow(view), watch.error,
    suggestedList(view));
  view.refreshWatchlist = async (focusKey) => {
    await renderWatchlist(view, lists, own);
    watch.wrap.querySelector(`[data-key="${focusKey}"]`)?.focus();
  };
  renderWatchlist(view, lists, own);
  view.sections.get("sources").replaceChildren(view.sectionHead(section),
    h("div", { class: "fields" }, feeds.wrap, watch.wrap, usajobsBlock(view)));
  view.showError("sources");
  view.showError("watchlist");
}

const describeCompany = (spec) => [h("span", { class: "spec-company", text: spec.company || spec.location }),
  h("span", { class: "spec-name mono", text: spec.location })];
const boardKey = (spec) => `${spec.kind}:${spec.location.toLowerCase()}`;

/** The starter lists, each with one switch for all its companies, then the companies of no list. */
async function renderWatchlist(view, listsBox, ownBox) {
  view.starterLists ??= api("/api/watchlist/starters", { quiet: true }).catch(() => []);
  const lists = await view.starterLists;
  if (!view.mounted || !listsBox.isConnected) return;
  const watchlist = view.draft.watchlist || [];
  const listed = new Set();
  listsBox.replaceChildren(...lists.map((list) => {
    const boards = new Set(list.boards.map(boardKey));
    const members = watchlist.flatMap((spec, index) => (boards.has(boardKey(spec)) ? [index] : []));
    for (const index of members) listed.add(index);
    return starterList(view, list, members);
  }));
  const rest = watchlist.flatMap((_, index) => (listed.has(index) ? [] : [index]));
  ownBox.replaceChildren(
    h("h4", { class: "field-label", text: "Your companies" }),
    rest.length ? specList(view, "watchlist", describeCompany, rest)
      : h("p", { class: "spec-empty muted", text: "Companies you add yourself show here." }));
}

function starterList(view, list, members) {
  view.openLists ??= new Set();
  const watchlist = view.draft.watchlist;
  const on = members.filter((index) => watchlist[index].enabled !== false).length;
  const all = on === list.boards.length;
  const open = view.openLists.has(list.id);
  const companies = h("div", { class: "starter-list-companies", hidden: !open },
    members.length ? specList(view, "watchlist", describeCompany, members)
      : h("p", { class: "muted", text: list.companies.join(", ") }));
  const expand = h("button", { type: "button", class: "starter-list-name", "aria-expanded": String(open),
    onclick: () => {
      const now = companies.hidden;
      companies.hidden = !now;
      expand.setAttribute("aria-expanded", String(now));
      if (now) view.openLists.add(list.id);
      else view.openLists.delete(list.id);
    } }, h("span", { text: list.name }), h("span", { class: "chevron", "aria-hidden": "true" }));
  const toggle = switchControl(null, all, (want) => setList(view, list, members, want), { key: `list-${list.id}` });
  toggle.querySelector("input").setAttribute("aria-label", `Follow every company in ${list.name}`);
  return h("section", { class: `starter-list${on ? "" : " spec-off"}` },
    h("div", { class: "starter-list-head" },
      h("div", { class: "starter-list-text" }, expand, h("span", { class: "muted", text: list.about })),
      h("span", { class: "starter-list-count mono", text: `${on} of ${list.boards.length} on` }),
      toggle),
    companies);
}

/** Follow every company of a starter list, or turn off the ones followed; Save keeps it. */
async function setList(view, list, members, want) {
  if (want) {
    try {
      const got = await api(`/api/watchlist/starters/${list.id}`, { method: "POST", quiet: true,
        body: { watchlist: view.draft.watchlist || [] } });
      if (!view.mounted) return;
      view.set("watchlist", got.watchlist);
    } catch (error) {
      toast(`Couldn't add ${list.name}: ${error.message}`, { tone: "error" });
    }
  } else {
    const next = clone(view.draft.watchlist);
    for (const index of members) next[index].enabled = false;
    view.set("watchlist", next);
  }
  renderSources(view);
  toast(`${want ? "Turned on" : "Turned off"} ${list.name}. Save to keep the change`);
}

/** Save the watchlist as a file to share, or add the companies from one. */
function sharingRow(view) {
  const merge = async (url, body, from) => {
    try {
      const got = await api(url, { method: "POST", quiet: true, body: { ...body, watchlist: view.draft.watchlist || [] } });
      if (!view.mounted) return;
      view.set("watchlist", got.watchlist);
      renderSources(view);
      const changed = got.added + got.turned_on;
      toast(changed ? `Added ${fmt.plural(changed, "company", "companies")} from ${got.name || from}. Save to keep the change`
        : `You already follow every company in ${got.name || from}`);
    } catch (error) {
      toast(`Couldn't add ${from}: ${error.message}`, { tone: "error" });
    }
  };
  const exportList = async () => {
    const data = await api("/api/watchlist/export").catch(() => null);
    if (!data) return;
    const href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const link = h("a", { href, download: "cairn-watchlist.json", hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1000);
    toast(`Saved ${fmt.plural(data.companies.length, "company", "companies")} to cairn-watchlist.json`);
  };
  const file = h("input", { type: "file", accept: ".json,application/json", hidden: true });
  file.addEventListener("change", async () => {
    const chosen = file.files?.[0];
    file.value = "";
    if (chosen) merge("/api/watchlist/import", { text: await chosen.text() }, chosen.name);
  });
  return h("div", { class: "share-row" },
    h("button", { type: "button", class: "btn btn-sm", text: "Save as a file", onclick: exportList,
      title: "Save the companies you follow as a file to share. Save your changes first to include them." }),
    h("button", { type: "button", class: "btn btn-sm", text: "Add from a file", onclick: () => file.click(),
      title: "Add the companies from a watchlist file someone shared" }),
    file);
}

/** Companies with strong recent postings and no watchlist board, each with Follow. */
function suggestedList(view) {
  const box = h("div", { class: "suggested" }, h("h4", { class: "field-label", text: "Suggested" }),
    h("p", { class: "muted", text: "Looking for companies with several strong postings…" }));
  api("/api/insights/companies", { quiet: true }).then((rows) => {
    if (!view.mounted) return;
    const list = view.draft.watchlist || [];
    const entry = (row) => list[followedAt(list, { company: row.company })];
    const fresh = rows.filter((row) => !entry(row) || entry(row).enabled === false);
    box.replaceChildren(h("h4", { class: "field-label", text: "Suggested" }),
      h("p", { class: "field-help", text: "Companies with three or more postings scoring 80+ for fit in the last 60 days that you don't follow yet." }),
      fresh.length ? h("ul", { class: "spec-list" }, fresh.map((row) => {
        const off = Boolean(entry(row));
        const follow = h("button", { type: "button", class: "btn btn-sm",
          title: off ? "You follow this company, but it's turned off" : "Follow this company" },
        icon("plus"), off ? "Turn on" : "Follow");
        follow.addEventListener("click", () => followSuggested(view, row.company, follow));
        return h("li", { class: "spec-row suggested-row" },
          h("span", { class: "spec-company", text: row.company }),
          h("span", { class: "spec-name mono", text: `${fmt.plural(row.postings, "strong posting")} · best fit ${row.best_fit}` }),
          follow);
      })) : h("p", { class: "muted", text: "No suggestions right now." }));
  }, (error) => {
    box.querySelector(".muted").textContent = `Couldn't load suggestions: ${error.message}`;
  });
  return box;
}

/** Follow a suggested company in the draft; Save keeps it. */
async function followSuggested(view, company, button) {
  button.disabled = true;
  try {
    const { spec, watchlist, turnedOn } = await followCompany(company, { watchlist: view.draft.watchlist || [] });
    if (!view.mounted) return;
    view.set("watchlist", watchlist);
    renderSources(view);
    toast(`${turnedOn ? "Turned on" : "Added"} ${spec.company || company}. Save to keep the change`);
  } catch (error) {
    button.disabled = false;
    toast(error.status === 404 ? `${error.message}. Paste a link to its careers page under Watchlist.`
      : error.status === 409 ? error.message : `Couldn't look it up: ${error.message}`, { tone: "error" });
  }
}

/** The USAJOBS email setting and its key, which the API keeps out of config.toml. */
function usajobsBlock(view) {
  const status = h("span", { class: "key-status muted", text: "Checking…" });
  const input = h("input", { type: "password", class: "input input-text", id: "usajobs-key", autocomplete: "new-password",
    "data-1p-ignore": true, "data-lpignore": "true", spellcheck: "false", placeholder: "Paste your key" });
  const save = h("button", { type: "button", class: "btn btn-sm", text: "Save key" });
  const clear = h("button", { type: "button", class: "btn btn-sm", text: "Clear", disabled: true });
  const show = (stored) => {
    status.textContent = stored ? "Key saved" : "No key yet";
    clear.disabled = !stored;
    input.placeholder = stored ? "Paste a new key to replace it" : "Paste your key";
  };
  api("/api/keys", { quiet: true }).then((keys) => show(Boolean(keys.usajobs)), () => {
    status.textContent = "Couldn't check for a saved key";
  });
  save.addEventListener("click", async () => {
    const key = input.value.trim();
    if (!key) {
      input.focus();
      return;
    }
    save.disabled = true;
    try {
      const keys = await api("/api/keys/usajobs", { method: "PUT", body: { key } });
      input.value = "";
      show(Boolean(keys.usajobs));
      toast("Saved the USAJOBS key");
    } catch {
      // api() has shown the error; the typed key stays for another try
    } finally {
      save.disabled = false;
    }
  });
  clear.addEventListener("click", async () => {
    clear.disabled = true;
    try {
      const keys = await api("/api/keys/usajobs", { method: "DELETE" });
      show(Boolean(keys.usajobs));
      toast("Removed the USAJOBS key");
    } catch {
      clear.disabled = false;
    }
  });
  return h("div", { class: "field usajobs" },
    h("h3", { class: "field-label", text: "USAJOBS" }),
    h("p", { class: "field-help" }, "To search USAJOBS, request a free key at ",
      h("a", { class: "link", href: "https://developer.usajobs.gov/apirequest/", target: "_blank", rel: "noopener noreferrer",
        text: "developer.usajobs.gov" }), ". USAJOBS emails it to you. Then enter the key and the email you used."),
    view.field(USAJOBS_EMAIL),
    h("div", { class: "field" }, h("label", { class: "field-label", for: "usajobs-key", text: "USAJOBS key" }),
      h("div", { class: "provider-key" }, input, save, clear), status));
}

/** Rows for the entries of view.draft[key] at indices, every entry by default. */
function specList(view, key, describe, indices) {
  const specs = view.draft[key] || [];
  if (!specs.length) {
    return h("p", { class: "spec-empty muted", text: key === "watchlist" ? "No companies yet." : "No sources yet. Add one so Cairn has jobs to find." });
  }
  return h("ul", { class: "spec-list" }, (indices ?? [...specs.keys()]).map((index) => {
    const spec = specs[index];
    const name = specName(spec);
    const label = spec.company || name;
    const row = h("li", { class: `spec-row${spec.enabled === false ? " spec-off" : ""}` },
      sourceChip(name), describe(spec),
      switchControl(null, spec.enabled !== false, (on) => {
        const next = clone(view.draft[key]);
        if (on) delete next[index].enabled;
        else next[index].enabled = false;
        row.classList.toggle("spec-off", !on);
        view.set(key, next);
        if (key === "watchlist") view.refreshWatchlist?.(`${key}-${index}`);
      }, { key: `${key}-${index}` }),
      h("button", { type: "button", class: "icon-btn", title: "Remove", "aria-label": `Remove ${label}`,
        onclick: () => {
          view.set(key, view.draft[key].filter((_, i) => i !== index));
          renderSources(view);
          toast(`Removed ${label}. Save to keep the change`);
        } }, icon("trash")));
    row.querySelector("input[role=switch]").setAttribute("aria-label", `Fetch ${label}`);
    return row;
  }));
}

function addSourceForm(view) {
  const kind = h("select", { class: "select", "aria-label": "Source kind" },
    view.kinds.map((value) => h("option", { value, text: kindLabel(value) })));
  const location = h("input", { type: "text", class: "input input-text", placeholder: KIND_HINTS.github,
    "aria-label": "Source location", spellcheck: "false" });
  const company = h("input", { type: "text", class: "input input-text source-company", placeholder: "Company name",
    "aria-label": "Company", hidden: true });
  const help = h("p", { class: "field-help add-note", hidden: true, text: WORKDAY_HELP });
  kind.addEventListener("change", () => {
    location.placeholder = KIND_HINTS[kind.value] || "";
    help.hidden = kind.value !== "workday";
    company.hidden = kind.value !== "page";
  });
  const form = h("form", { class: "add-row", onsubmit: (event) => {
    event.preventDefault();
    const value = location.value.trim();
    if (!value) return;
    const spec = { kind: kind.value, location: value };
    if (kind.value === "page") {
      if (!company.value.trim()) {
        company.focus();
        return;
      }
      spec.company = company.value.trim();
    }
    view.set("sources", [...(view.draft.sources || []), spec]);
    renderSources(view);
    view.sections.get("sources").querySelector(".add-row select")?.focus();
  } }, kind, location, company, h("button", { type: "submit", class: "btn btn-sm" }, icon("plus"), "Add source"));
  return h("div", {}, form, help);
}

function addCompanyForm(view) {
  const query = h("input", { type: "text", class: "input input-text", placeholder: "Company name or careers page link",
    "aria-label": "Company name or careers page link", spellcheck: "false" });
  const note = h("p", { class: "field-help add-note", "aria-live": "polite" });
  const button = h("button", { type: "submit", class: "btn btn-sm" }, icon("plus"), "Add company");
  const form = h("form", { class: "add-row", onsubmit: async (event) => {
    event.preventDefault();
    const text = query.value.trim();
    if (!text) return;
    button.disabled = true;
    note.textContent = "Looking for its careers page…";
    try {
      const { spec, watchlist, turnedOn } = await followCompany(text, { watchlist: view.draft.watchlist || [] });
      if (!view.mounted) return;
      view.set("watchlist", watchlist);
      renderSources(view);
      toast(`${turnedOn ? "Turned on" : "Added"} ${spec.company || text}. Save to keep the change`);
    } catch (error) {
      note.textContent = error.status === 404 ? `${error.message}. Paste a link to its careers page instead.`
        : error.status === 409 ? `${error.message}.` : `Couldn't look it up: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  } }, query, button);
  return h("div", {}, form, note);
}
