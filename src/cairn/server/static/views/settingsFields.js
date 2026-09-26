/**
 * @typedef {object} Field
 * @property {string} key       the config.toml setting
 * @property {string} label
 * @property {string} help
 * @property {"tags" | "number" | "switch" | "text"} type
 * @property {string} [unit]
 * @property {boolean} [nullable] an empty number means null
 * @property {number} [min]
 * @property {number} [max]
 * @property {boolean} [claudeOnly] shown only while Claude Code is the AI provider
 * @property {boolean} [macOnly] shown only on a Mac
 */

/** @type {{id: string, title: string, fields?: Field[]}[]} */
export const SECTIONS = [
  { id: "profile", title: "Profile" },
  { id: "preferences", title: "Preferences", fields: [
    { key: "title_keywords", label: "Title keywords", type: "tags",
      help: "Cairn shows only postings whose title has one of these words." },
    { key: "title_exclude", label: "Skip titles with", type: "tags",
      help: "Cairn hides postings whose title has any of these words. The defaults skip senior, staff, lead and manager roles." },
    { key: "title_exclude_field", label: "Skip field roles with", type: "tags",
      help: "Cairn hides hardware and field jobs that the word “engineer” would let in." },
    { key: "allowed_categories", label: "Categories", type: "tags",
      help: "Job categories to keep. A posting needs one of these and a title keyword." },
    { key: "location_allow", label: "Locations", type: "tags",
      help: "Leave empty to see postings in every location." },
    { key: "degrees_held", label: "Degrees", type: "tags",
      help: "Cairn hides postings that accept none of your degrees. Leave empty to show them all." },
    { key: "graduation_year", label: "Graduation year", type: "number", nullable: true, min: 2000, max: 2100,
      help: "Cairn flags postings that start before you graduate. Leave empty to skip the flag." },
    { key: "wanted_intern_terms", label: "Internship terms", type: "tags",
      help: "Cairn shows internships only for these terms, such as “Fall 2026”. Leave empty to hide every internship." },
    { key: "include_off_season_internships", label: "Internships", type: "switch",
      help: "Turn off to hide every internship. When on, Cairn shows internships for the terms above." },
    { key: "intern_terms", label: "Internship words", type: "tags",
      help: "Words in a title that mark a posting as an internship." },
    { key: "recent_days", label: "Recent days", type: "number", unit: "days", min: 1,
      help: "Cairn skips postings that haven't been posted or updated in this many days." },
  ] },
  { id: "sources", title: "Sources" },
  { id: "icons", title: "Company icons", fields: [
    { key: "company_icons", label: "Fetch icons", type: "switch",
      help: "Cairn shows each company's icon next to its postings." },
  ] },
  { id: "provider", title: "AI provider" },
  { id: "ranking", title: "Ranking", fields: [
    { key: "fit_threshold", label: "Minimum fit", type: "number", unit: "0–100", min: 0, max: 100,
      help: "Postings below this fit score get no summary and no phone alert." },
    { key: "tier_floor", label: "Minimum company tier", type: "number", unit: "0–100", min: 0, max: 100,
      help: "Postings from companies below this score stay in the list but get no summary or phone alert." },
    { key: "rank_batch_size", label: "Postings per batch", type: "number", unit: "postings", min: 1,
      help: "How many postings Cairn sends the AI at once. Lower it if ranking keeps failing." },
    { key: "rank_retries", label: "Retries", type: "number", unit: "tries", min: 0,
      help: "How many times Cairn tries a batch again when it can't read the answer." },
    { key: "max_rank_per_run", label: "Max ranked per run", type: "number", unit: "postings", nullable: true, min: 1,
      help: "Leave empty to rank every new posting. With a limit, Cairn ranks the newest first and saves the rest for later runs. The first run ranks every match either way." },
    { key: "max_summaries_per_run", label: "Max summaries per run", type: "number", unit: "postings", min: 0,
      help: "Each summary uses one AI request." },
    { key: "monthly_call_cap", label: "Monthly AI limit", type: "number", unit: "requests", nullable: true, min: 1,
      help: "Cairn stops ranking and summarizing after this many AI requests in 30 days. Leave empty for no limit." },
    { key: "fetch_descriptions", label: "Summarize requirements", type: "switch",
      help: "Reads the posting pages of your best matches and summarizes what they ask for." },
    { key: "claude_model", label: "Ranking model", type: "text", claudeOnly: true,
      help: "The Claude Code model Cairn ranks postings with, such as sonnet." },
    { key: "description_model", label: "Summary model", type: "text", claudeOnly: true,
      help: "The Claude Code model Cairn summarizes posting pages with, such as haiku. A cheaper model is enough here." },
    { key: "model_concurrency", label: "Requests at once", type: "number", unit: "requests", min: 1, max: 8,
      help: "How many AI requests Cairn sends at the same time. A higher number finishes a run sooner. Lower it if your AI provider says you sent too many." },
    { key: "claude_bin", label: "Claude command", type: "text",
      help: "Where Cairn finds Claude Code. Leave it alone unless Checkup can't find Claude Code." },
  ] },
  { id: "notifications", title: "Notifications", fields: [
    { key: "notify_ntfy_topic", label: "Phone alerts", type: "text",
      help: "The ntfy topic your phone subscribes to. Pick a name only you know, or leave it empty to turn alerts off." },
    { key: "notify_macos", label: "Mac notifications", type: "switch", macOnly: true,
      help: "Show a banner on this Mac when a run finishes." },
  ] },
  { id: "schedule", title: "Schedule" },
  { id: "system", title: "System" },
  { id: "doctor", title: "Checkup" },
  { id: "advanced", title: "Advanced" },
];
/** @type {Field} shown in Sources, beside the USAJOBS key */
export const USAJOBS_EMAIL = { key: "usajobs_email", label: "USAJOBS email", type: "text",
  help: "The email you used to get your USAJOBS key." };
