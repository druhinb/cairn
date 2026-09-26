const SVG = "http://www.w3.org/2000/svg";

// 16×16 viewBox, stroked at 1.5px in currentColor
const PATHS = {
  jobs: "M2.5 5.5h11v8h-11zM5.5 5.5v-2h5v2M2.5 9h11",
  latest: "M8 1.75v3M8 11.25v3M1.75 8h3M11.25 8h3M3.6 3.6l2 2M10.4 10.4l2 2M3.6 12.4l2-2M10.4 5.6l2-2",
  saved: "M4 2.5h8v11L8 10.5l-4 3z",
  applications: "M2.5 2.5h3v11h-3zM6.5 2.5h3v7h-3zM10.5 2.5h3v9h-3z",
  runs: "M4.5 3v10l8.5-5z",
  settings: "M2.5 4.5h11M2.5 11.5h11M5.5 3v3M10.5 10v3",
  search: "M7 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM10.75 10.75l3 3",
  x: "M4 4l8 8M12 4l-8 8",
  chevronDown: "M4 6l4 4 4-4",
  chevronLeft: "M10 4L6 8l4 4",
  chevronRight: "M6 4l4 4-4 4",
  pin: "M5.5 2.5h5M6.5 2.5v4L4.5 9h7l-2-2.5v-4M8 9v4.5",
  external: "M9 2.5h4.5V7M13.5 2.5L7.5 8.5M11.5 9.5v4h-9v-9h4",
  more: "M3.5 8h.01M8 8h.01M12.5 8h.01",
  pass: "M3 3l10 10M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11z",
  applied: "M2.5 8l11-5.5-3.5 11-2.5-4.5z",
  panelRight: "M2.5 2.5h11v11h-11zM10 2.5v11",
  panelLeft: "M2.5 2.5h11v11h-11zM6 2.5v11",
  sun: "M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1",
  moon: "M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7z",
  auto: "M2 3h12v8H2zM6 14h4M8 11v3",
  help: "M6 6a2 2 0 1 1 2.8 1.8c-.5.3-.8.7-.8 1.2V10M8 12.5h.01",
  copy: "M5.5 5.5h8v8h-8zM10.5 5.5v-3h-8v8h3",
  play: "M5 3.5v9l7-4.5z",
  board: "M2.5 2.5h11v11h-11zM6.2 2.5v11M9.8 2.5v11",
  list: "M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h.01M2.5 8h.01M2.5 12h.01",
  plus: "M8 3v10M3 8h10",
  trash: "M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.5 9h6l.5-9",
  check: "M3 8.5l3 3 7-7",
  circle: "M8 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  upload: "M8 10.5V2.5M5 5.5l3-3 3 3M2.5 10.5v3h11v-3",
  refresh: "M13 3.5v3h-3M13 6.5A5 5 0 1 0 12 11",
  download: "M8 2.5v8M5 7.5l3 3 3-3M2.5 10.5v3h11v-3",
  contrast: "M8 13.5a5.5 5.5 0 1 0 0-11 5.5 5.5 0 0 0 0 11zM8 2.5v11M8 5h3.5M8 8h5M8 11h3.5",
  rows: "M2.5 3.5h11M2.5 8h11M2.5 12.5h11",
  rowsCompact: "M2.5 3h11M2.5 6.3h11M2.5 9.7h11M2.5 13h11",
  today: "M2.5 3.5h11v10h-11zM2.5 6.5h11M5.5 2v3M10.5 2v3M5.5 9.5h2v2h-2z",
  insights: "M2.5 13.5h11M4 11V8M7 11V4.5M10 11V6.5M13 11V3",
  thumbUp: "M5 7.5v6H2.5v-6zM5 7.5l2.5-5c1 0 1.5.7 1.5 1.5V6.5h3.5a1 1 0 0 1 1 1.2l-1 5a1 1 0 0 1-1 .8H5",
  thumbDown: "M5 8.5v-6H2.5v6zM5 8.5l2.5 5c1 0 1.5-.7 1.5-1.5V9.5h3.5a1 1 0 0 0 1-1.2l-1-5a1 1 0 0 0-1-.8H5",
  calendar: "M2.5 3.5h11v10h-11zM2.5 6.5h11M5.5 2v3M10.5 2v3",
  arrowUp: "M8 13V3M4 7l4-4 4 4",
  arrowDown: "M8 3v10M4 9l4 4 4-4",
  grip: "M6 4h.01M10 4h.01M6 8h.01M10 8h.01M6 12h.01M10 12h.01",
  pencil: "M10.5 3l2.5 2.5-7 7H3.5V10zM9 4.5l2.5 2.5",
  note: "M3.5 2.5h9v11h-9zM5.5 5.5h5M5.5 8h5M5.5 10.5h3",
  file: "M4 2.5h5l3 3v8H4zM9 2.5v3h3",
};

/**
 * A 16px inline SVG icon. Decorative unless `label` is given.
 * @param {keyof typeof PATHS} name @param {string} [label] @returns {SVGSVGElement}
 */
export function icon(name, label) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("class", `icon icon-${name}`);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", PATHS[name]);
  svg.append(path);
  return svg;
}
