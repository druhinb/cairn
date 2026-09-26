const PROPERTIES = new Set(["value", "checked", "disabled", "hidden", "indeterminate"]);
const DAY_MS = 86400000;

/**
 * Build an element. `on*` props become listeners, `class`/`text` set className and
 * textContent, null/false props and children are skipped.
 * @param {string} tag
 * @param {Record<string, any>} [props]
 * @param {...any} children
 * @returns {HTMLElement}
 */
export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.style.cssText = value;
    else if (PROPERTIES.has(key)) node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat(Infinity).filter((child) => child != null && child !== false));
  return node;
}

/** @param {string} url @returns {string | null} the url when it is http(s), else null */
export function safeUrl(url) {
  try {
    return ["http:", "https:"].includes(new URL(url).protocol) ? url : null;
  } catch {
    return null;
  }
}

/** @template {(...args: any[]) => void} F @param {F} fn @param {number} ms @returns {F & {cancel: () => void}} */
export function debounce(fn, ms) {
  let timer;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

const STEPS = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };

/**
 * Move focus among `items` for an arrow, Home or End key, leaving only the focused
 * item in the tab order.
 * @param {KeyboardEvent} event @param {HTMLElement[]} items
 * @returns {HTMLElement | null} the item now focused, or null when the key is not a move
 */
export function rove(event, items) {
  if (!items.length || event.altKey || event.ctrlKey || event.metaKey) return null;
  const current = items.indexOf(/** @type {HTMLElement} */ (event.target));
  let next;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = items.length - 1;
  else if (STEPS[event.key]) next = (Math.max(current, 0) + STEPS[event.key] + items.length) % items.length;
  else return null;
  event.preventDefault();
  items.forEach((item, i) => { item.tabIndex = i === next ? 0 : -1; });
  items[next].focus();
  return items[next];
}

/** @param {EventTarget | null} target @returns {boolean} */
export function isTyping(target) {
  return target instanceof HTMLElement
    && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

/** Unix seconds (feed dates) or an ISO string (store timestamps) as a Date, or null. */
function toDate(value) {
  if (value == null || value === "") return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * Whole calendar days between value and today.
 * @param {number | string | null | undefined} value @returns {number | null} null when not a date
 */
export function daysAgo(value) {
  const date = toDate(value);
  if (!date) return null;
  return Math.round((startOfDay(new Date()) - startOfDay(date)) / DAY_MS);
}

const CURRENCY_SYMBOLS = { USD: "$", EUR: "€", GBP: "£", CAD: "C$" };
const PAY_FULL_FROM = 10000000;
// toLocaleString builds a formatter on every call, which took 25 ms across 1,000 rows
const MONTH_DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const FULL = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "short" });
const NUMBER = new Intl.NumberFormat();

/** Display formats for dates, times and counts; a missing or invalid value renders "–". */
export const fmt = {
  /** "today", "2d", "3w", then "Aug 2" past eight weeks. */
  age(value) {
    const days = daysAgo(value);
    if (days == null) return "–";
    if (days <= 0) return "today";
    if (days < 14) return `${days}d`;
    if (days < 56) return `${Math.floor(days / 7)}w`;
    return MONTH_DAY.format(toDate(value));
  },
  /** "12m" or "5h" for a time in the last day, else null. */
  since(value) {
    const date = toDate(value);
    const minutes = date ? Math.floor((Date.now() - date.getTime()) / 60000) : -1;
    if (minutes < 0 || minutes >= 24 * 60) return null;
    return minutes < 60 ? `${minutes}m` : `${Math.floor(minutes / 60)}h`;
  },
  date(value) {
    const date = toDate(value);
    return date ? date.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" }) : "–";
  },
  /** "Sep 24", with the year when it is not this one. */
  day(value) {
    const date = toDate(value);
    if (!date) return "–";
    const year = date.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year });
  },
  time(value) {
    const date = toDate(value);
    return date ? date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
      : "–";
  },
  dateTime(value) {
    const date = toDate(value);
    return date ? date.toLocaleString(undefined,
      { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "–";
  },
  /** The full local date and time, for the tooltip of a relative age or a short date. */
  full(value) {
    const date = toDate(value);
    return date ? FULL.format(date) : null;
  },
  /** Today's times as "7:04 AM", older ones as "Sep 22, 7:04 AM". */
  when(value) {
    return daysAgo(value) === 0 ? fmt.time(value) : fmt.dateTime(value);
  },
  /** "0s", "42s", "3m 4s", "1h 12m". */
  duration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return "–";
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
  number(n) {
    return n == null ? "–" : NUMBER.format(Number(n));
  },
  plural(n, one, many = `${one}s`) {
    return `${fmt.number(n)} ${n === 1 ? one : many}`;
  },
  /**
   * "$120–150k", "$25.50/h", "$130k+", "up to €90k"; null when no amount is stated.
   * Yearly and monthly amounts from 1,000 up to 9,999,999 show in thousands, larger
   * ones in full; hourly pay shows cents when either end has them. A range whose ends
   * read the same shows one amount, and an inverted one shows low to high. An
   * unknown currency shows its code, a missing one no symbol.
   * @param {{min: number | null, max: number | null, period?: string | null, currency?: string | null} | null} salary
   * @returns {string | null}
   */
  pay(salary) {
    if (!salary || (salary.min == null && salary.max == null)) return null;
    const code = String(salary.currency || "").toUpperCase();
    const symbol = code ? CURRENCY_SYMBOLS[code] ?? `${code} ` : "";
    const hourly = salary.period === "hour";
    const amounts = [salary.min, salary.max].filter((n) => n != null).sort((a, b) => a - b);
    const thousands = !hourly && amounts.every((n) => n >= 1000 && n < PAY_FULL_FROM);
    const cents = hourly && amounts.some((n) => !Number.isInteger(n));
    const num = (n) => {
      if (hourly) return cents ? n.toFixed(2) : fmt.number(n);
      return thousands ? String(Math.round(n / 100) / 10) : fmt.number(Math.round(n));
    };
    const unit = `${thousands ? "k" : ""}${hourly ? "/h" : salary.period === "month" ? "/mo" : ""}`;
    if (amounts.length === 2) {
      const [low, high] = amounts.map(num);
      return low === high ? `${symbol}${low}${unit}` : `${symbol}${low}–${high}${unit}`;
    }
    const [only] = amounts;
    return salary.min != null ? `${symbol}${num(only)}${unit}+` : `up to ${symbol}${num(only)}${unit}`;
  },
};
