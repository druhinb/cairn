import { api } from "../lib/api.js";
import { debounce, fmt, h } from "../lib/dom.js";
import { subscribe } from "../lib/events.js";
import { followCompany } from "../lib/follow.js";
import { kindLabel, sourceChip } from "../components/chip.js";
import { icon } from "../components/icons.js";
import { pixelArt } from "../components/pixelart.js";
import { STATUS_LABELS } from "../components/pill.js";
import { toast } from "../components/toast.js";

const SVG = "http://www.w3.org/2000/svg";
const SKILLS_SHOWN = 20;
const FUNNEL = ["saved", "applied", "interviewing", "offer", "rejected", "withdrawn"];
const FUNNEL_TONES = { saved: "blue", applied: "violet", interviewing: "ok", offer: "good", rejected: "weak",
  withdrawn: "grey" };
const RELOAD_DELAY_MS = 300;
const SPARK_W = 240;
const SPARK_H = 48;

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

/** "the same day", "1 day", "12 days" */
function daysText(n) {
  return n === 0 ? "the same day" : fmt.plural(n, "day");
}

/** "840 characters", "12k characters" */
function charsText(n) {
  return n < 1000 ? fmt.plural(n, "character") : `${fmt.number(Math.round(n / 1000))}k characters`;
}

/** A line of weekly counts with a dot on the last week, scaled to the largest week. */
function sparkline(weeks, key, label) {
  const values = weeks.map((week) => week[key] || 0);
  const top = Math.max(1, ...values);
  const step = values.length > 1 ? SPARK_W / (values.length - 1) : 0;
  const points = values.map((value, i) => [i * step, SPARK_H - 4 - (value / top) * (SPARK_H - 8)]);
  const chart = svg("svg", { viewBox: `-4 0 ${SPARK_W + 8} ${SPARK_H}`, width: SPARK_W + 8, height: SPARK_H,
    class: `spark spark-${key}`, role: "img", "aria-label": label });
  chart.append(svg("line", { x1: 0, x2: SPARK_W, y1: SPARK_H - 4, y2: SPARK_H - 4, class: "spark-base" }),
    svg("polyline", { points: points.map(([x, y]) => `${x},${y}`).join(" "), class: "spark-line" }));
  const [lastX, lastY] = points.at(-1) || [0, SPARK_H - 4];
  chart.append(svg("circle", { cx: lastX, cy: lastY, r: 2.5, class: "spark-dot" }));
  return chart;
}

function bar(label, value, top, tone, text, title) {
  return h("li", { class: "bar-row", title },
    h("span", { class: "bar-label", text: label }),
    h("span", { class: "bar-track", "aria-hidden": "true" },
      h("span", { class: `bar-fill bar-${tone}`, style: `width: ${value ? Math.max(3, (value / top) * 100) : 0}%` })),
    h("span", { class: "bar-value mono", text }));
}

class Insights {
  constructor(root, ctx) {
    this.root = root;
    this.ctx = ctx;
    this.mounted = true;
    this.cleanups = [];
    this.reloadSoon = debounce(() => this.loadStats(), RELOAD_DELAY_MS);
  }

  mount() {
    this.panels = {
      skills: this.panel("skills", "Skills gap", "Skills your best matches ask for that your profile lacks."),
      companies: this.panel("companies", "Companies to follow", "Companies with three or more postings scoring 80+ for fit in the last 60 days that you don't follow yet."),
      funnel: this.panel("funnel", "Application funnel", "Every application you tracked, by the furthest status it reached."),
      cost: this.panel("cost", "AI requests", "Requests Cairn sent to the AI provider in the last 30 days."),
    };
    this.root.replaceChildren(h("section", { class: "view insights-view" },
      h("header", { class: "view-head" }, h("div", { class: "view-heading" }, h("h1", { class: "display", text: "Insights" }))),
      h("div", { class: "view-scroll insights-body" }, Object.values(this.panels).map((panel) => panel.element))));
    this.cleanups.push(
      () => { this.mounted = false; },
      () => this.reloadSoon.cancel(),
      subscribe("application_changed", () => this.reloadSoon()),
      subscribe("tracking_changed", () => this.reloadSoon()),
      subscribe("summary_done", () => this.loadSkills()),
      subscribe("run_done", () => {
        this.loadSkills();
        this.loadCompanies();
        this.loadCost();
      }));
    this.loadSkills();
    this.loadCompanies();
    this.loadStats();
    this.loadCost();
    return () => this.cleanups.forEach((fn) => fn());
  }

  panel(key, title, hint) {
    const body = h("div", { class: "insights-panel-body" }, h("p", { class: "muted", text: "Loading…" }));
    const element = h("section", { class: `panel insights-panel insights-${key}`, "aria-labelledby": `insights-${key}-title` },
      h("div", { class: "panel-head" }, h("h2", { class: "panel-title", id: `insights-${key}-title`, text: title }),
        h("p", { class: "panel-hint", text: hint })),
      body);
    return { element, body };
  }

  fill(key, ...children) {
    if (this.mounted) this.panels[key].body.replaceChildren(...children.flat().filter(Boolean));
  }

  failed(key, noun, error, retry) {
    this.fill(key, h("p", { class: "field-error", text: `Couldn't load ${noun}: ${error.message}` }),
      h("button", { type: "button", class: "btn btn-sm", text: "Try again", onclick: retry }));
  }

  empty(key, art, title, hint) {
    this.fill(key, h("div", { class: "today-empty" }, pixelArt(art),
      h("p", { class: "today-empty-title display", text: title }), h("p", { class: "muted", text: hint })));
  }

  async loadSkills() {
    let rows;
    try {
      rows = await api("/api/insights/skills", { quiet: true });
    } catch (error) {
      this.failed("skills", "the skills", error, () => this.loadSkills());
      return;
    }
    const missing = rows.filter((row) => row.missing_in > 0).slice(0, SKILLS_SHOWN);
    if (!missing.length) {
      this.empty("skills", "checklist", "No gaps yet", "Skills come from the summaries of your best matches. Start a run, or open a posting and choose Summarize requirements.");
      return;
    }
    const top = Math.max(...missing.map((row) => row.missing_in));
    this.fill("skills", h("ol", { class: "bars" }, missing.map((row) => bar(row.skill, row.missing_in, top, "weak",
      `${fmt.number(row.missing_in)} missing · ${fmt.number(row.met_in)} met`,
      `Missing in ${row.missing_in} postings, met in ${row.met_in}`))));
  }

  async loadCompanies() {
    let rows;
    try {
      rows = await api("/api/insights/companies", { quiet: true });
    } catch (error) {
      this.failed("companies", "the companies", error, () => this.loadCompanies());
      return;
    }
    if (!rows.length) {
      this.empty("companies", "magnifier", "No companies to add", "No company you don't follow has three postings scoring 80+ for fit in the last 60 days.");
      return;
    }
    this.fill("companies", h("ul", { class: "company-rows" }, rows.map((row) => this.companyRow(row))));
  }

  companyRow(row) {
    const action = h("span", { class: "company-action" });
    const follow = h("button", { type: "button", class: "btn btn-sm" }, icon("plus"), row.followed ? "Turn on" : "Follow");
    follow.title = row.followed ? "You follow this company, but it's turned off" : "Follow this company";
    follow.addEventListener("click", () => this.follow(row, follow, action));
    action.append(follow);
    return h("li", { class: "company-row" },
      h("span", { class: "company-name", text: row.company, "data-company": row.company }),
      h("span", { class: "company-facts mono", text: `${fmt.plural(row.postings, "posting")} · best fit ${row.best_fit}${row.mean_tier != null ? ` · tier ${row.mean_tier}` : ""}` }),
      action);
  }

  /** Add the company's board to the watchlist, or turn its disabled board back on, and save at once. */
  async follow(row, button, action) {
    button.disabled = true;
    try {
      const { spec, turnedOn } = await followCompany(row.company, { save: true });
      toast(`${turnedOn ? "Turned on" : "Following"} ${spec.company || row.company} through its ${kindLabel(spec.kind)} board`);
      action.replaceChildren(h("span", { class: "company-followed" }, icon("check"), "Following"),
        sourceChip(`${spec.kind}:${spec.location}`));
    } catch (error) {
      button.disabled = false;
      toast(error.status === 404 ? `${error.message}. Add its board URL in Settings › Sources.`
        : error.status === 409 ? error.message : `Couldn't follow ${row.company}: ${error.message}`, { tone: "error" });
    }
  }

  async loadStats() {
    let stats;
    try {
      stats = await api("/api/insights/stats", { quiet: true });
    } catch (error) {
      this.failed("funnel", "the application stats", error, () => this.loadStats());
      return;
    }
    const funnel = stats.funnel || {};
    const total = FUNNEL.reduce((sum, status) => sum + (funnel[status] || 0), 0);
    if (!total) {
      this.empty("funnel", "plane", "No applications yet", "Save a posting or mark one applied in Jobs to start the funnel.");
      return;
    }
    const top = Math.max(...FUNNEL.map((status) => funnel[status] || 0));
    const response = stats.response_days || {};
    const weeks = stats.weekly || [];
    const replyText = response.n
      ? `Half of the replies came within ${daysText(response.median)}, three in four within ${daysText(response.p75)} (${fmt.plural(response.n, "reply", "replies")}).`
      : "No replies yet. Reply times start once an application moves past Applied.";
    this.fill("funnel",
      h("ol", { class: "bars funnel" }, FUNNEL.map((status) => bar(STATUS_LABELS[status], funnel[status] || 0, top,
        FUNNEL_TONES[status], fmt.number(funnel[status] || 0)))),
      h("p", { class: "insights-line", text: replyText }),
      weeks.length > 0 && h("div", { class: "weekly" },
        h("div", { class: "weekly-head" }, h("span", { class: "section-label", text: "Applied per week" }),
          h("span", { class: "muted mono", text: `${fmt.day(`${weeks[0].week}T00:00`)} – now` })),
        sparkline(weeks, "applied", `Applications sent per week over ${weeks.length} weeks: ${weeks.map((week) => week.applied).join(", ")}`),
        h("p", { class: "muted", text: `${fmt.number(weeks.reduce((sum, week) => sum + week.applied, 0))} applied · ${fmt.number(weeks.reduce((sum, week) => sum + week.interviewing, 0))} interviews · ${fmt.number(weeks.reduce((sum, week) => sum + week.offers, 0))} offers in ${weeks.length} weeks` })),
      (stats.by_source || []).length > 0 && h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
        h("thead", {}, h("tr", {}, ["Source", "Applied", "Interviews", "Offers"].map((label, i) => h("th", { scope: "col",
          class: i ? "num" : null, text: label })))),
        h("tbody", {}, stats.by_source.map((row) => h("tr", {},
          h("td", {}, row.source ? sourceChip(row.source) : "–"),
          h("td", { class: "num mono", text: fmt.number(row.applied) }),
          h("td", { class: "num mono", text: fmt.number(row.interviewing) }),
          h("td", { class: "num mono", text: fmt.number(row.offers) })))))));
  }

  async loadCost() {
    let cost;
    let cap = null;
    try {
      const [counts, cfg] = await Promise.all([api("/api/insights/cost?days=30", { quiet: true }),
        // without the settings the panel shows no cap
        api("/api/settings", { quiet: true }).catch(() => null)]);
      cost = counts;
      cap = cfg?.monthly_call_cap ?? null;
    } catch (error) {
      this.failed("cost", "the call counts", error, () => this.loadCost());
      return;
    }
    const top = Math.max(1, cost.rank, cost.summary, cost.onboard);
    const share = cap ? Math.min(1, cost.total / cap) : null;
    this.fill("cost",
      h("div", { class: "today-big" }, h("span", { class: "today-number display", text: fmt.number(cost.total) }),
        h("span", { class: "today-number-label", text: cap ? `of your ${fmt.number(cap)}-request limit` : "requests, no limit set" })),
      share != null && h("span", { class: "bar-track bar-wide", role: "img", "aria-label": `${Math.round(share * 100)}% of the limit used` },
        h("span", { class: `bar-fill ${share >= 0.9 ? "bar-weak" : share >= 0.6 ? "bar-ok" : "bar-good"}`, style: `width: ${Math.max(2, share * 100)}%` })),
      h("ol", { class: "bars" }, [["rank", "Ranking"], ["summary", "Summaries"], ["onboard", "Setup"]]
        .map(([key, label]) => bar(label, cost[key], top, "blue", fmt.number(cost[key])))),
      h("p", { class: "muted", text: `${charsText(cost.prompt_chars || 0)} sent to the AI provider.` }),
      h("a", { class: "link", href: "#settings?section=ranking", text: cap ? "Change the limit in Settings › Ranking" : "Set a limit in Settings › Ranking" }));
  }
}

/**
 * The Insights view: skills gap, companies to follow, the application funnel and
 * the month's model calls.
 * @param {HTMLElement} root
 * @param {object} ctx  see app.js `viewContext`
 * @returns {() => void} unmount
 */
export function mount(root, ctx) {
  return new Insights(root, ctx).mount();
}
