/**
 * The first run's progress, built from the run's events or, after a reload, from its
 * log so far. No DOM, so the run screen and its tests share it.
 */

const STAMP = /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d {2}/;
const PHASE_LINE = /^== (\w+) ==$/;
const RANK_LINE = /^\[rank\] fit (\d+) tier (\d+)(\s+below floor)?/;
const FAILED_LINE = /^run \d+ failed: (.*)$/;
const SOURCE_LINE = /^\[fetch\] (.+): (\d+) postings/;
const FETCH_LINE = /^\[fetch\] (\d+) total, (\d+) relevant\/recent, (\d+) new/;
const LINKS_LINE = /^\[links\] checked (\d+) new, (\d+) closed/;
const SUMMARY_LINE = /^\[jd\] (\d+)\/(\d+) postings summarised/;
const RANK_PROGRESS_LINE = /^\[rank\] ranked (\d+) of (\d+)$/;
const SUMMARY_PROGRESS_LINE = /^\[jd\] summarised (\d+) of (\d+)$/;

/** The screen's steps in order; `step` in a progress names the one under way. */
export const STEPS = ["fetch", "match", "rank", "read"];

/**
 * @typedef {object} Progress
 * @property {"running" | "done" | "failed"} status
 * @property {string} step         one of STEPS, or "done"
 * @property {number} sources      sources that answered
 * @property {number} fetched      postings they listed
 * @property {number | null} matched  postings that pass the preferences
 * @property {number | null} fresh    of those, the ones no run has ranked
 * @property {number | null} toRank   the share this run ranks
 * @property {[number, number] | null} rankProgress     [done, total] postings sent to the model so far
 * @property {number} ranked
 * @property {number} strong       ranked at or over the fit threshold, above the tier floor
 * @property {[number, number] | null} summaryProgress  [done, total] summaries tried so far
 * @property {[number, number] | null} summarised  [made, tried]
 * @property {string | null} error
 * @property {number} unscored   postings sent to the model that a finished run left unranked, when it ranked none
 * @property {Record<string, number> | null} counts  run_done's counts
 * @property {number | null} fitThreshold
 */

/** @param {number | null} fitThreshold @returns {Progress} */
export function newProgress(fitThreshold) {
  return { status: "running", step: "fetch", sources: 0, fetched: 0, matched: null, fresh: null,
    toRank: null, rankProgress: null, ranked: 0, strong: 0, summaryProgress: null, summarised: null, error: null, unscored: 0, counts: null, fitThreshold };
}

function readInfo(progress, text) {
  const fetched = text.match(FETCH_LINE);
  const source = !fetched && text.match(SOURCE_LINE);
  const links = text.match(LINKS_LINE);
  const summary = text.match(SUMMARY_LINE);
  if (source) {
    progress.sources += 1;
    progress.fetched += Number(source[2]);
  } else if (fetched) {
    Object.assign(progress, { matched: Number(fetched[2]), fresh: Number(fetched[3]), step: "match" });
    progress.toRank ??= progress.fresh;
  } else if (links && progress.toRank != null) {
    progress.toRank = Math.max(0, progress.toRank - Number(links[2]));
  } else if (summary) {
    progress.summarised = [Number(summary[1]), Number(summary[2])];
  }
}

/**
 * Fold one run event into the progress.
 * @param {Progress} progress @param {string} kind @param {Record<string, any>} data
 * @returns {Progress} the same object
 */
export function advance(progress, kind, data) {
  if (progress.status !== "running") return progress;
  if (kind === "phase_start" && data.name === "rank") {
    progress.step = "rank";
  } else if (kind === "phase_end" && data.name === "rank") {
    progress.step = "done";
  } else if (kind === "rank_progress") {
    progress.rankProgress = [data.done, data.total];
  } else if (kind === "summary_progress") {
    progress.summaryProgress = [data.done, data.total];
  } else if (kind === "posting_ranked") {
    // a run announces its ranked postings once ranking ends and before it summarises
    progress.ranked = Math.min(progress.ranked + 1, progress.toRank ?? Infinity);
    if (progress.fitThreshold != null && data.fit >= progress.fitThreshold && !data.below_floor) progress.strong += 1;
    progress.step = "read";
  } else if (kind === "info" && typeof data.text === "string") {
    readInfo(progress, data.text);
  } else if (kind === "run_done" && !data.counts?.ranked && data.counts?.unranked) {
    // every ranking call failed, and the run still finished with nothing to show
    Object.assign(progress, { status: "failed", step: "rank", unscored: data.counts.unranked, counts: data.counts });
  } else if (kind === "run_done") {
    Object.assign(progress, { status: "done", step: "done", counts: data.counts || {} });
  } else if (kind === "run_failed") {
    Object.assign(progress, { status: "failed", error: data.error || null });
  }
  return progress;
}

/**
 * One run.log line as the event that wrote it, or null for a line the progress
 * does not read.
 * @param {string} line @returns {[string, Record<string, any>] | null}
 */
export function lineEvent(line) {
  const text = line.replace(STAMP, "");
  const phase = text.match(PHASE_LINE);
  if (phase) return ["phase_start", { name: phase[1] }];
  const ranked = text.match(RANK_LINE);
  if (ranked) return ["posting_ranked", { fit: Number(ranked[1]), tier: Number(ranked[2]), below_floor: Boolean(ranked[3]) }];
  const rankProgress = text.match(RANK_PROGRESS_LINE);
  if (rankProgress) return ["rank_progress", { done: Number(rankProgress[1]), total: Number(rankProgress[2]) }];
  const summaryProgress = text.match(SUMMARY_PROGRESS_LINE);
  if (summaryProgress) return ["summary_progress", { done: Number(summaryProgress[1]), total: Number(summaryProgress[2]) }];
  const failed = text.match(FAILED_LINE);
  if (failed) return ["run_failed", { error: failed[1] }];
  if (text.startsWith("[")) return ["info", { text }];
  return null;
}

/**
 * The progress of a run from its log so far, for a page that opened mid-run.
 * @param {Progress} progress @param {string} log @returns {Progress}
 */
export function replay(progress, log) {
  for (const line of log.split("\n")) {
    const event = line && lineEvent(line);
    if (event) advance(progress, ...event);
  }
  return progress;
}
