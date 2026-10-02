import assert from "node:assert/strict";
import { test } from "node:test";
import { QUIZ_CARDS, quizCards, statedAuthorization } from "../../src/cairn/server/static/lib/quiz.js";

const CHOICES = ["US citizen", "F-1 OPT", "needs sponsorship", "unknown"];

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

test("the stated authorization is the answer setup wrote, else the line itself", () => {
  const profile = "## Snapshot\n- Sam Lee.\n- Work authorization: F-1 OPT.\n\n## Target roles\n";
  assert.equal(statedAuthorization(profile, CHOICES), "F-1 OPT");
  const restated = profile.replace("OPT.\n", "OPT.\n  Stated during setup: US citizen.\n");
  assert.equal(statedAuthorization(restated, CHOICES), "US citizen");
  assert.equal(statedAuthorization("- Work authorization: **U.S. Citizen**\n", CHOICES), "unknown");
  assert.equal(statedAuthorization("# Profile\n", CHOICES), "unknown");
});

test("advanced comes last when it is on, and roles go without choices", () => {
  const cards = quizCards({ jobType: "both", roles: false, advanced: true });
  assert.equal(cards.at(-1), "advanced");
  assert.ok(!cards.includes("roles"));
  assert.equal(cards[0], "jobs");
});
