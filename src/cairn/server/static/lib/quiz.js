/** Every card of setup's preference questions, in the order they are asked. */
export const QUIZ_CARDS = ["jobs", "roles", "places", "school", "experience", "terms", "recent", "tiers", "advanced"];

/**
 * The cards to ask: experience only for full-time jobs, terms only for
 * internships, roles only when there are roles to pick, and advanced only when
 * it is turned on.
 * @param {{jobType: string, roles: boolean, advanced: boolean}} answers
 * @returns {string[]}
 */
export function quizCards({ jobType, roles, advanced }) {
  const skipped = new Set([jobType === "internships" && "experience", jobType === "new_grad" && "terms",
    !roles && "roles", !advanced && "advanced"]);
  return QUIZ_CARDS.filter((card) => !skipped.has(card));
}

const AUTHORIZATION_LINE = /work authori[sz]ation/i;
const STATED_LINE = /^\s+Stated during setup:/;

/**
 * The work authorization profile.md states, as one of `choices`, or "unknown"
 * when it states none of them. An answer setup wrote under the line wins.
 * @param {string} profile @param {string[]} choices
 * @returns {string}
 */
export function statedAuthorization(profile, choices) {
  const lines = profile.split("\n");
  const at = lines.findIndex((line) => AUTHORIZATION_LINE.test(line));
  if (at < 0) return "unknown";
  const said = (STATED_LINE.test(lines[at + 1] || "") ? lines[at + 1] : lines[at]).toLowerCase();
  return choices.find((choice) => choice !== "unknown" && said.includes(choice.toLowerCase())) || "unknown";
}
