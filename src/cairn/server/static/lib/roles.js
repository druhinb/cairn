/**
 * @typedef {{title_keywords: string[], title_exclude: string[], title_exclude_field: string[]}} Lists
 * @typedef {object} Options  /api/onboard/options
 * @property {Record<string, {keywords: string[], unskips: string[]}>} roles
 * @property {Record<string, {list: "title_exclude" | "title_exclude_field", words: string[]}>} skips
 * @typedef {{roles: string[], skips: string[]}} Checked  the boxes checked before a change
 */

/** @param {Options} options @param {string[]} roles */
const unskippedBy = (options, roles) => new Set(roles.flatMap((role) => options.roles[role].unskips));

/**
 * The title lists with one of setup's roles checked or unchecked. Checking adds the
 * role's keywords and takes its unskips off the field skip list; unchecking undoes
 * that for each word no other checked role also brings, and puts an unskip back
 * only while a checked skip group holds it.
 * @param {Lists} lists @param {Options} options @param {Checked} checked
 * @param {string} role @param {boolean} on
 * @returns {Partial<Lists>}
 */
export function withRole(lists, options, checked, role, on) {
  const { keywords, unskips } = options.roles[role];
  if (on) {
    return { title_keywords: [...new Set([...lists.title_keywords, ...keywords])],
      title_exclude_field: lists.title_exclude_field.filter((word) => !unskips.includes(word)) };
  }
  const others = checked.roles.filter((other) => other !== role);
  const kept = new Set(others.flatMap((other) => options.roles[other].keywords));
  const stillUnskipped = unskippedBy(options, others);
  const skipped = new Set(checked.skips.flatMap((group) => options.skips[group].words));
  return { title_keywords: lists.title_keywords.filter((word) => !keywords.includes(word) || kept.has(word)),
    title_exclude_field: [...new Set([...lists.title_exclude_field,
      ...unskips.filter((word) => !stillUnskipped.has(word) && skipped.has(word))])] };
}

/**
 * The title lists with one of setup's skip groups checked or unchecked. Checking
 * adds the group's words to its list, less the ones a checked role takes off;
 * unchecking takes out each word no other checked group also holds.
 * @param {Lists} lists @param {Options} options @param {Checked} checked
 * @param {string} group @param {boolean} on
 * @returns {Partial<Lists>}
 */
export function withSkip(lists, options, checked, group, on) {
  const { list, words } = options.skips[group];
  if (on) {
    const unskipped = unskippedBy(options, checked.roles);
    return { [list]: [...new Set([...lists[list], ...words.filter((word) => !unskipped.has(word))])] };
  }
  const kept = new Set(checked.skips.filter((other) => other !== group && options.skips[other].list === list)
    .flatMap((other) => options.skips[other].words));
  return { [list]: lists[list].filter((word) => !words.includes(word) || kept.has(word)) };
}

/**
 * The skip groups whose words the lists already hold, counting a word a checked
 * role takes off as held.
 * @param {Lists} lists @param {Options} options @param {string[]} roles
 */
export function heldSkips(lists, options, roles) {
  const unskipped = unskippedBy(options, roles);
  return Object.entries(options.skips)
    .filter(([, { list, words }]) => words.every((word) => lists[list].includes(word) || unskipped.has(word)))
    .map(([group]) => group);
}
