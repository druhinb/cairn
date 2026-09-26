// a spreadsheet runs a cell that starts with one of these as a formula
const FORMULA_START = /^[=+\-@\t\r]/;

const unguard = (value) => (value[0] === "'" && FORMULA_START.test(value.slice(1)) ? value.slice(1) : value);

/**
 * Split RFC 4180 text into rows of fields: comma-separated, CRLF or LF line ends,
 * fields in double quotes may hold commas, line breaks and "" for a quote. Blank
 * lines are dropped, and one leading ' comes off a field that formatCsv guarded.
 * Throws when a quoted field never closes.
 * @param {string} text @returns {string[][]}
 */
export function parseCsv(text) {
  const src = text.replace(/^\uFEFF/, "");
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let wasQuoted = false;
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (quoted) {
      if (ch !== '"') field += ch;
      else if (src[i + 1] === '"') {
        field += '"';
        i += 1;
      } else quoted = false;
    } else if (ch === '"' && field === "" && !wasQuoted) {
      quoted = true;
      wasQuoted = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
      wasQuoted = false;
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && src[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      wasQuoted = false;
    } else {
      field += ch;
    }
  }
  if (quoted) throw new Error("a quoted field never closes");
  if (field !== "" || row.length || wasQuoted) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((fields) => fields.some((value) => value.trim() !== ""))
    .map((fields) => fields.map(unguard));
}

/**
 * Join rows into RFC 4180 text with CRLF line ends, quoting the fields that hold a
 * comma, a quote, a line break, or space at either end. A field a spreadsheet would
 * run as a formula gets a leading '.
 * @param {(string | number | null | undefined)[][]} rows @returns {string}
 */
export function formatCsv(rows) {
  const cell = (value) => {
    const raw = value == null ? "" : String(value);
    const text = FORMULA_START.test(raw) ? `'${raw}` : raw;
    return /[",\r\n]|^\s|\s$/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return rows.map((row) => row.map(cell).join(",")).join("\r\n") + "\r\n";
}
