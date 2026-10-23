// Authored line icons. One stroke weight, 24px grid, currentColor.
const SVG = "http://www.w3.org/2000/svg";
const SHAPES = {
  inbox: [
    ["path", { d: "M3.5 13.5h4l1.6 3h5.8l1.6-3h4" }],
    ["path", { d: "m5.7 5.5-2.2 8v5h17v-5l-2.2-8h-12.6Z" }],
  ],
  close: [["path", { d: "m6 6 12 12M18 6 6 18" }]],
  arrow: [["path", { d: "M5 12h13M13 6l6 6-6 6" }]],
  search: [
    ["circle", { cx: "10.8", cy: "10.8", r: "6.3" }],
    ["path", { d: "m16 16 4 4" }],
  ],
  dot: [["circle", { cx: "12", cy: "12", r: "3.5" }]],
};

export function icon(name, size = 18) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of SHAPES[name] || SHAPES.dot) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    svg.appendChild(node);
  }
  return svg;
}
