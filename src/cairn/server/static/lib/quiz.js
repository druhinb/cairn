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
