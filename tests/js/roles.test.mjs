import assert from "node:assert/strict";
import { test } from "node:test";
import { withRole } from "../../src/cairn/server/static/lib/roles.js";

const ROLES = {
  backend: { keywords: ["backend", "engineer"], unskips: [] },
  systems: { keywords: ["infrastructure", "engineer"], unskips: [] },
  embedded: { keywords: ["embedded", "firmware"], unskips: ["firmware"] },
};
const LISTS = { title_keywords: ["quant", "backend"], title_exclude_field: ["hardware", "firmware"] };

test("checking a role adds its keywords once and takes its words off the skip list", () => {
  assert.deepEqual(withRole(LISTS, ROLES, [], "embedded", true),
    { title_keywords: ["quant", "backend", "embedded", "firmware"], title_exclude_field: ["hardware"] });
  assert.deepEqual(withRole(LISTS, ROLES, [], "backend", true).title_keywords, ["quant", "backend", "engineer"]);
});

test("unchecking a role keeps the keywords another checked role has", () => {
  const lists = { title_keywords: ["backend", "engineer", "infrastructure"], title_exclude_field: [] };
  assert.deepEqual(withRole(lists, ROLES, ["backend", "systems"], "backend", false).title_keywords,
    ["engineer", "infrastructure"]);
});

test("unchecking a role puts its skip words back unless another checked role takes them off", () => {
  const lists = { title_keywords: ["embedded", "firmware"], title_exclude_field: ["hardware"] };
  assert.deepEqual(withRole(lists, ROLES, ["embedded"], "embedded", false),
    { title_keywords: [], title_exclude_field: ["hardware", "firmware"] });
});
