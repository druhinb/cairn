import assert from "node:assert/strict";
import { test } from "node:test";
import { advance, newProgress, replay } from "../../src/cairn/server/static/lib/firstRun.js";

const LOG = `2026-09-25 18:00:00  run 3 started  limit=None  fit=None  dry_run=False  first_run=True
2026-09-25 18:00:00  == fetch ==
2026-09-25 18:00:01  [fetch] SimplifyJobs/New-Grad-Positions: 1200 postings.
2026-09-25 18:00:02  [fetch] Stripe: 40 postings, 2 already seen in an earlier source.
2026-09-25 18:00:03  [fetch] 1240 total, 300 relevant/recent, 300 new since last run.
2026-09-25 18:00:03  [fetch] 3.0s
2026-09-25 18:00:03  [first run] 100 of 300 new postings to rank, the newest from the last 14 days. The other 200 are marked seen.
2026-09-25 18:00:05  [links] checked 100 new, 4 closed and left out of ranking
2026-09-25 18:00:05  == rank ==
`;
const RANKED = `2026-09-25 18:02:00  [rank] fit 85 tier 70  Acme — Software Engineer  abc
2026-09-25 18:02:00  [rank] fit 90 tier 20  below floor  Initech — Software Engineer  def
2026-09-25 18:02:00  [rank] fit 40 tier 80  Globex — Software Engineer  ghi
`;

test("a log read mid-rank gives the sources, the matches and the share being ranked", () => {
  const progress = replay(newProgress(60), LOG);
  assert.equal(progress.step, "rank");
  assert.deepEqual([progress.sources, progress.fetched], [2, 1240]);
  assert.deepEqual([progress.matched, progress.fresh, progress.toRank], [300, 300, 96]);
  assert.equal(progress.status, "running");
});

test("ranked lines end the ranking and count the strong ones above the tier floor", () => {
  const progress = replay(newProgress(60), LOG + RANKED);
  assert.equal(progress.step, "read");
  assert.deepEqual([progress.ranked, progress.strong], [3, 1]);
});

test("a run without a first-run plan ranks every new posting it found", () => {
  const progress = newProgress(60);
  advance(progress, "info", { text: "[fetch] 50 total, 12 relevant/recent, 7 new since last run." });
  assert.deepEqual([progress.step, progress.toRank], ["match", 7]);
});

test("the summary line and run_done finish it, and later events change nothing", () => {
  const progress = replay(newProgress(60), LOG + RANKED);
  advance(progress, "info", { text: "[jd] 1/1 postings summarised from their real description." });
  advance(progress, "phase_end", { name: "rank", seconds: 12 });
  advance(progress, "run_done", { counts: { ranked: 3 } });
  advance(progress, "posting_ranked", { fit: 99, tier: 99, below_floor: false });
  assert.deepEqual(progress.summarised, [1, 1]);
  assert.deepEqual([progress.status, progress.step, progress.ranked], ["done", "done", 3]);
  assert.deepEqual(progress.counts, { ranked: 3 });
});

test("a failure in the log is read back with its error", () => {
  const progress = replay(newProgress(60), `${LOG}2026-09-25 18:01:00  run 3 failed: RuntimeError: claude is down\n`);
  assert.deepEqual([progress.status, progress.error], ["failed", "RuntimeError: claude is down"]);
});

test("an unknown fit threshold counts nothing as strong", () => {
  const progress = replay(newProgress(null), LOG + RANKED);
  assert.deepEqual([progress.ranked, progress.strong], [3, 0]);
});

test("a run that finishes with every posting unranked is a failure", () => {
  const progress = replay(newProgress(60), LOG);
  advance(progress, "phase_end", { name: "rank", seconds: 30 });
  advance(progress, "run_done", { counts: { ranked: 0, unranked: 96 } });
  assert.deepEqual([progress.status, progress.step, progress.unscored], ["failed", "rank", 96]);
});

test("a run with nothing new to rank is done", () => {
  const progress = newProgress(60);
  advance(progress, "run_done", { counts: { ranked: 0, unranked: 0 } });
  assert.deepEqual([progress.status, progress.unscored], ["done", 0]);
});

test("rank and summary progress lines give the live counts after a reload", () => {
  const log = `${LOG}2026-09-25 18:00:30  [rank] ranked 50 of 96\n`;
  const ranking = replay(newProgress(60), log);
  assert.deepEqual([ranking.step, ranking.rankProgress], ["rank", [50, 96]]);
  const reading = replay(newProgress(60), `${log}${RANKED}2026-09-25 18:02:10  [jd] summarised 1 of 3\n`);
  assert.deepEqual([reading.step, reading.summaryProgress], ["read", [1, 3]]);
});

test("progress events update the counts as they arrive", () => {
  const progress = replay(newProgress(60), LOG);
  advance(progress, "rank_progress", { done: 25, total: 96 });
  advance(progress, "rank_progress", { done: 50, total: 96 });
  assert.deepEqual(progress.rankProgress, [50, 96]);
});
