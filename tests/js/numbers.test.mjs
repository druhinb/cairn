import assert from "node:assert/strict";
import { test } from "node:test";
import { readNumber } from "../../src/cairn/server/static/lib/numbers.js";

const typed = (value, badInput = false) => ({ value, validity: { badInput } });

test("a whole number inside the range reads as that number", () => {
  assert.deepEqual(readNumber(typed(" 90 "), { min: 1, max: 365 }), { value: 90 });
});

test("an empty field is null only when the field allows it", () => {
  assert.deepEqual(readNumber(typed(""), { nullable: true }), { value: null });
  assert.deepEqual(readNumber(typed(""), {}), { error: "Enter a number" });
});

test("a fraction, a number out of range or unreadable text is refused with a reason", () => {
  assert.deepEqual(readNumber(typed("2.5"), { min: 1 }), { error: "Enter a whole number" });
  assert.deepEqual(readNumber(typed("0"), { min: 1, max: 8 }), { error: "Enter a whole number from 1 to 8" });
  assert.deepEqual(readNumber(typed("0"), { min: 1 }), { error: "Enter a whole number of at least 1" });
  assert.deepEqual(readNumber(typed("", true), { min: 1 }), { error: "Enter a number" });
});
