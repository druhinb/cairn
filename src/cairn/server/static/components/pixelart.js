const SVG = "http://www.w3.org/2000/svg";

// 16×16 grids, "#" is a lit pixel; each run of pixels in a row becomes one rect
const ART = {
  tray: [
    "................",
    "................",
    "...##########...",
    "...#........#...",
    "...#.######.#...",
    "...#........#...",
    "...#.####...#...",
    "..##........##..",
    ".#............#.",
    "#..............#",
    "#####......#####",
    "#....#....#....#",
    "#.....####.....#",
    "#..............#",
    "################",
    "................",
  ],
  magnifier: [
    "................",
    "....#####.......",
    "...#.....#......",
    "..#..##...#.....",
    "..#.#.....#.....",
    "..#.......#.....",
    "..#.......#.....",
    "..#.......#.....",
    "...#.....#......",
    "....#####.#.....",
    "..........###...",
    "...........###..",
    "............###.",
    ".............##.",
    "................",
    "................",
  ],
  sun: [
    "................",
    ".......##.......",
    "..#....##....#..",
    "...#........#...",
    "................",
    "......####......",
    ".....######.....",
    "##..########..##",
    "....########....",
    "################",
    "................",
    "..############..",
    "................",
    ".....######.....",
    "................",
    "................",
  ],
  plane: [
    "................",
    "..............#.",
    "............###.",
    "..........##.##.",
    "........##..#.#.",
    "......##...#..#.",
    "....##....#...#.",
    "..##.....#....#.",
    ".####...#.....#.",
    ".....####....#..",
    "......#..##..#..",
    "......#....###..",
    "......#...#.....",
    "......#.##......",
    "......##........",
    "................",
  ],
  checklist: [
    ".....######.....",
    "..###.####.###..",
    "..#..........#..",
    "..#.......##.#..",
    "..#..#...##..#..",
    "..#..##.##...#..",
    "..#...###....#..",
    "..#....#.....#..",
    "..#..........#..",
    "..#.##.#####.#..",
    "..#..........#..",
    "..#.##.#####.#..",
    "..#..........#..",
    "..#.##.####..#..",
    "..#..........#..",
    "..############..",
  ],
};

/**
 * A 16×16 pixel drawing for an empty state, in currentColor at 48px.
 * @param {keyof typeof ART} name @returns {SVGSVGElement}
 */
export function pixelArt(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "48");
  svg.setAttribute("height", "48");
  svg.setAttribute("class", `pixel-art pixel-${name}`);
  svg.setAttribute("shape-rendering", "crispEdges");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  ART[name].forEach((row, y) => {
    for (const run of row.matchAll(/#+/g)) svg.append(rect([run.index, y, run[0].length, 1]));
  });
  return svg;
}

function rect([x, y, width, height, className]) {
  const node = document.createElementNS(SVG, "rect");
  node.setAttribute("x", String(x));
  node.setAttribute("y", String(y));
  node.setAttribute("width", String(width));
  node.setAttribute("height", String(height));
  if (className) node.setAttribute("class", className);
  return node;
}

const GRID_CLASSES = { "#": "px-line", "-": "px-soft", "+": "px-accent" };

/** [x, y, width, 1, class] for each run of one character in a scene grid. */
function gridRects(rows) {
  return rows.flatMap((row, y) => [...row.matchAll(/#+|-+|\++/g)]
    .map((run) => [run.index, y, run[0].length, 1, GRID_CLASSES[run[0][0]]]));
}

// 32×24 grids: "#" is a line, "-" softer text, "+" the accent
const READING = [
  "................................",
  "................................",
  "................................",
  "..########.........########.....",
  "..#......##........#......##....",
  "..#.---..#.#.......#......#.#...",
  "..#......####......#......####..",
  "..#.........#......#.........#..",
  "..#.-------.#......#.........#..",
  "..#.........#......#.........#..",
  "..#.------..#.++...#.........#..",
  "..#.........#..++..#.........#..",
  "..#.-------.#...++.#.........#..",
  "..#.........#..++..#.........#..",
  "..#.-----...#.++...#.........#..",
  "..#.........#......#.........#..",
  "..#.-------.#......#.........#..",
  "..#.........#......#.........#..",
  "..#.----....#......#.........#..",
  "..#.........#......#.........#..",
  "..###########......###########..",
  "................................",
  "................................",
  "................................",
];

const RANKING = [
  "................................",
  "................................",
  "...##########################...",
  "...#........................#...",
  "...#..---..#########........#...",
  "...#..---...................#...",
  "...#..---..------...........#...",
  "...#........................#...",
  "...#..---..#######..........#...",
  "...#..---...................#...",
  "...#..---..------...........#...",
  "...#........................#...",
  "...#..---..########.........#...",
  "...#..---...................#...",
  "...#..---..------...........#...",
  "...#........................#...",
  "...#..---..######...........#...",
  "...#..---...................#...",
  "...#..---..------...........#...",
  "...#........................#...",
  "...#........................#...",
  "...##########################...",
  "................................",
  "................................",
];

/**
 * Each scene is its layers in paint order, [part, rects], every rect [x, y, width,
 * height, class?]. A part is one group the stylesheet colours and moves; repeated
 * parts carry their index in data-i and the first rect's width in --w.
 */
const SCENES = {
  // a resume read line by line and a profile written beside it
  reading: [
    ["grid", gridRects(READING)],
    ["scan", [[3, 7, 9, 1]]],
    ...[[21, 5, 3], [21, 8, 7], [21, 10, 6], [21, 12, 7], [21, 14, 5], [21, 16, 7], [21, 18, 4]]
      .map(([x, y, width]) => ["write", [[x, y, width, 1]]]),
  ],
  // a list whose rows are scored in turn, each score lighting in its fit colour
  ranking: [
    ["cursor", [[4, 3, 24, 5, "px-band"], [4, 3, 1, 5, "px-accent"]]],
    ["grid", gridRects(RANKING)],
    ...[4, 8, 12, 16].map((y) => ["chip", [[22, y, 4, 3]]]),
  ],
};

/**
 * A 32×24 scene for the first-run screens at 4×, in currentColor. Its motion is in
 * the stylesheet, so reduced motion shows the still picture.
 * @param {keyof typeof SCENES} name @returns {SVGSVGElement}
 */
export function pixelScene(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 32 24");
  svg.setAttribute("width", "128");
  svg.setAttribute("height", "96");
  svg.setAttribute("class", `pixel-scene scene-${name}`);
  svg.setAttribute("shape-rendering", "crispEdges");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const counts = new Map();
  for (const [part, rects] of SCENES[name]) {
    const group = document.createElementNS(SVG, "g");
    const index = counts.get(part) ?? 0;
    counts.set(part, index + 1);
    group.setAttribute("class", `scene-${part}`);
    group.setAttribute("data-i", String(index));
    group.style.setProperty("--i", String(index));
    group.style.setProperty("--w", String(rects[0][2]));
    group.append(...rects.map(rect));
    svg.append(group);
  }
  return svg;
}
