import { api } from "./api.js";

const sameName = (a, b) => Boolean(a && b) && a.toLowerCase() === b.toLowerCase();

/**
 * The watchlist entry that follows a company: the same board, or an entry under
 * the same company name.
 * @param {object[]} watchlist @param {{company?: string, kind?: string, location?: string}} spec
 * @returns {number} its index, or -1
 */
export function followedAt(watchlist, spec) {
  return watchlist.findIndex((known) => (spec.kind && known.kind === spec.kind && known.location === spec.location)
    || sameName(known.company, spec.company));
}

/**
 * Follow a company: look up its job board and add it to the watchlist, or turn its
 * disabled entry back on. `watchlist` is the list to add to, such as a settings
 * draft; without one the saved watchlist is read. `save` writes the result through
 * PUT /api/settings at once.
 * Rejects with the lookup's error (status 404 when no board was found), or with
 * status 409 when an enabled entry already follows the company.
 * @param {string} company
 * @param {{watchlist?: object[] | null, save?: boolean}} [options]
 * @returns {Promise<{spec: object, watchlist: object[], turnedOn: boolean}>}
 */
export async function followCompany(company, { watchlist = null, save = false } = {}) {
  const [found, list] = await Promise.all([
    api("/api/watchlist/resolve", { method: "POST", body: { query: company }, quiet: true }),
    watchlist ? Promise.resolve(watchlist) : api("/api/settings", { quiet: true }).then((cfg) => cfg.watchlist || [])]);
  const index = followedAt(list, { ...found, company: found.company || company });
  if (index >= 0 && list[index].enabled !== false) {
    const error = new Error(`You already follow ${list[index].company || found.company || company}`);
    error.status = 409;
    throw error;
  }
  let next = [...list, found];
  if (index >= 0) {
    const on = { ...list[index] };
    delete on.enabled;
    next = list.map((known, i) => (i === index ? on : known));
  }
  if (save) await api("/api/settings", { method: "PUT", body: { watchlist: next }, quiet: true });
  return { spec: index >= 0 ? next[index] : found, watchlist: next, turnedOn: index >= 0 };
}
