/**
 * The title lists with one of setup's roles checked or unchecked. Checking adds the
 * role's keywords and takes its unskips off the field skip list; unchecking undoes
 * that for each word no other checked role also brings.
 * @param {{title_keywords: string[], title_exclude_field: string[]}} lists
 * @param {Record<string, {keywords: string[], unskips: string[]}>} roles  /api/onboard/options roles
 * @param {string[]} checked  the roles checked before this change
 * @param {string} role @param {boolean} on
 */
export function withRole(lists, roles, checked, role, on) {
  const { keywords, unskips } = roles[role];
  if (on) {
    return { title_keywords: [...new Set([...lists.title_keywords, ...keywords])],
      title_exclude_field: lists.title_exclude_field.filter((word) => !unskips.includes(word)) };
  }
  const others = checked.filter((other) => other !== role).map((other) => roles[other]);
  const kept = new Set(others.flatMap((other) => other.keywords));
  const stillUnskipped = new Set(others.flatMap((other) => other.unskips));
  return { title_keywords: lists.title_keywords.filter((word) => !keywords.includes(word) || kept.has(word)),
    title_exclude_field: [...new Set([...lists.title_exclude_field,
      ...unskips.filter((word) => !stillUnskipped.has(word))])] };
}
