import assert from "node:assert/strict";
import { test } from "node:test";
import { formatCsv, parseCsv } from "../../src/cairn/server/static/lib/csv.js";

test("formula cells get a leading quote on export", () => {
  const text = formatCsv([["=1+1", "+SUM(A1)", "-2", "@cmd", "\tx", "\rx", "plain", 42]]);
  assert.equal(text, `'=1+1,'+SUM(A1),'-2,'@cmd,'\tx,"'\rx",plain,42\r\n`);
});

test("formula cells round trip unchanged", () => {
  const row = ["=HYPERLINK(\"x\")", "+1", "-", "@a,b", "\tlead", "\rcr", "'kept", "it's"];
  assert.deepEqual(parseCsv(formatCsv([row])), [row]);
});

test("a leading quote stays on a cell that is not a formula", () => {
  assert.deepEqual(parseCsv("'hello,'=x\n"), [["'hello", "=x"]]);
});

test("CRLF and LF line ends both split rows", () => {
  assert.deepEqual(parseCsv("a,b\r\nc,d\ne,f"), [["a", "b"], ["c", "d"], ["e", "f"]]);
});

test("quoted fields hold line breaks, commas and doubled quotes", () => {
  assert.deepEqual(parseCsv('id,note\r\n1,"two\r\nlines, and ""quotes"""\r\n'),
    [["id", "note"], ["1", 'two\r\nlines, and "quotes"']]);
  assert.deepEqual(parseCsv(formatCsv([["a\nb", "c"]])), [["a\nb", "c"]]);
});

test("a byte order mark before the header is dropped", () => {
  assert.deepEqual(parseCsv("﻿url,status\r\nhttps://x.test,applied\r\n"),
    [["url", "status"], ["https://x.test", "applied"]]);
});

test("blank lines are dropped and an unclosed quote throws", () => {
  assert.deepEqual(parseCsv("a\r\n\r\n,\r\nb\r\n"), [["a"], ["b"]]);
  assert.throws(() => parseCsv('a,"b\n'), /never closes/);
});
