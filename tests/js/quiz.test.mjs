import assert from "node:assert/strict";
import { test } from "node:test";
import { QUIZ_CARDS, quizCards } from "../../src/cairn/server/static/lib/quiz.js";

test("both job types ask every card but advanced", () => {
  assert.deepEqual(quizCards({ jobType: "both", roles: true, advanced: false }),
    QUIZ_CARDS.filter((card) => card !== "advanced"));
});

test("internships skip experience and new grad skips terms", () => {
  const interns = quizCards({ jobType: "internships", roles: true, advanced: false });
  assert.ok(interns.includes("terms"));
  assert.ok(!interns.includes("experience"));
  const grads = quizCards({ jobType: "new_grad", roles: true, advanced: false });
  assert.ok(grads.includes("experience"));
  assert.ok(!grads.includes("terms"));
});

test("advanced comes last when it is on, and roles go without choices", () => {
  const cards = quizCards({ jobType: "both", roles: false, advanced: true });
  assert.equal(cards.at(-1), "advanced");
  assert.ok(!cards.includes("roles"));
  assert.equal(cards[0], "jobs");
});
