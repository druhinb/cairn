import assert from "node:assert/strict";
import { test } from "node:test";
import { heldSkips, withRole, withSkip } from "../../src/cairn/server/static/lib/roles.js";

const OPTIONS = {
  roles: {
    backend: { keywords: ["backend", "engineer"], unskips: [] },
    systems: { keywords: ["infrastructure", "engineer"], unskips: [] },
    embedded: { keywords: ["embedded", "firmware"], unskips: ["firmware"] },
  },
  skips: {
    senior: { list: "title_exclude", words: ["senior", "staff"] },
    managers: { list: "title_exclude", words: ["manager"] },
    hardware: { list: "title_exclude_field", words: ["hardware", "firmware"] },
  },
};
const LISTS = { title_keywords: ["quant", "backend"], title_exclude: ["senior", "staff"],
  title_exclude_field: ["hardware", "firmware"] };
const NONE = { roles: [], skips: [] };

test("checking a role adds its keywords once and takes its words off the skip list", () => {
  assert.deepEqual(withRole(LISTS, OPTIONS, NONE, "embedded", true),
    { title_keywords: ["quant", "backend", "embedded", "firmware"], title_exclude_field: ["hardware"] });
  assert.deepEqual(withRole(LISTS, OPTIONS, NONE, "backend", true).title_keywords, ["quant", "backend", "engineer"]);
});

test("unchecking a role keeps the keywords another checked role has", () => {
  const lists = { ...LISTS, title_keywords: ["backend", "engineer", "infrastructure"] };
  const checked = { roles: ["backend", "systems"], skips: [] };
  assert.deepEqual(withRole(lists, OPTIONS, checked, "backend", false).title_keywords,
    ["engineer", "infrastructure"]);
});

test("unchecking a role puts its skip words back only while a checked group holds them", () => {
  const lists = { ...LISTS, title_keywords: ["embedded", "firmware"], title_exclude_field: ["hardware"] };
  assert.deepEqual(withRole(lists, OPTIONS, { roles: ["embedded"], skips: ["hardware"] }, "embedded", false),
    { title_keywords: [], title_exclude_field: ["hardware", "firmware"] });
  assert.deepEqual(withRole(lists, OPTIONS, { roles: ["embedded"], skips: [] }, "embedded", false).title_exclude_field,
    ["hardware"]);
});

test("checking a skip group adds its words to its own list, less what a checked role takes off", () => {
  assert.deepEqual(withSkip(LISTS, OPTIONS, NONE, "managers", true),
    { title_exclude: ["senior", "staff", "manager"] });
  const lists = { ...LISTS, title_exclude_field: [] };
  assert.deepEqual(withSkip(lists, OPTIONS, { roles: ["embedded"], skips: [] }, "hardware", true),
    { title_exclude_field: ["hardware"] });
});

test("unchecking a skip group takes out its words and leaves the rest", () => {
  const lists = { ...LISTS, title_exclude: ["senior", "staff", "manager", "intern"] };
  assert.deepEqual(withSkip(lists, OPTIONS, { roles: [], skips: ["senior", "managers"] }, "senior", false),
    { title_exclude: ["manager", "intern"] });
});

test("a group counts as checked when its list holds every word a checked role leaves", () => {
  assert.deepEqual(heldSkips(LISTS, OPTIONS, []), ["senior", "hardware"]);
  const lists = { ...LISTS, title_exclude_field: ["hardware"] };
  assert.deepEqual(heldSkips(lists, OPTIONS, []), ["senior"]);
  assert.deepEqual(heldSkips(lists, OPTIONS, ["embedded"]), ["senior", "hardware"]);
});
