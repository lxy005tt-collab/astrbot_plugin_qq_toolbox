// Tiny DOM helpers. All untrusted text goes through textContent — never innerHTML.
import { icon } from "./icons.js";

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null) continue;
      if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "text") el.textContent = value;
      else if (key === "html") throw new Error("innerHTML is not allowed");
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "style" && typeof value === "object") {
        Object.assign(el.style, value);
      } else if (key === "attrs") {
        for (const [a, v] of Object.entries(value)) {
          if (v !== undefined && v !== null && v !== false) el.setAttribute(a, String(v));
        }
      } else if (value !== false) {
        el.setAttribute(key, value === true ? "" : String(value));
      }
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function badge(text, kind = "") {
  return h("span", { class: `badge ${kind}`.trim(), text });
}

export function dot(kind = "") {
  return h("span", { class: `status-dot ${kind}`.trim() });
}

export function emptyState(title, detail) {
  return h(
    "div",
    { class: "empty" },
    h("span", { class: "empty-icon" }, icon("inbox", 26)),
    h("strong", { text: title }),
    detail ? h("div", { class: "tiny", text: detail }) : null,
  );
}

export function statCard({ label, value, sub, tone = "" }) {
  return h(
    "div",
    { class: `stat ${tone}`.trim() },
    h("div", { class: "label" }, h("span", { text: label })),
    h("div", { class: "value", text: value }),
    h("div", { class: "sub" }, sub || ""),
  );
}

export function kv(k, v) {
  return h("div", { class: "kv" }, h("div", { class: "k", text: k }), h("div", { class: "v" }, v));
}

export function button(text, onClick, { kind = "", small = true, disabled = false, title = "" } = {}) {
  return h("button", {
    class: `btn ${kind} ${small ? "small" : ""}`.trim(),
    text,
    title,
    disabled,
    onclick: onClick,
  });
}

// Minimal, safe text renderer: escapes by using text nodes; supports fenced code.
export function textBlock(text, { mono = false, klass = "" } = {}) {
  const wrap = h("div", { class: `text-block ${klass}`.trim() });
  if (!text) {
    wrap.appendChild(h("span", { class: "muted", text: "（空）" }));
    return wrap;
  }
  if (mono) {
    wrap.classList.add("mono");
    wrap.textContent = text;
    return wrap;
  }
  const parts = String(text).split(/```/);
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const code = part.replace(/^[a-zA-Z0-9_-]*\n/, "");
      wrap.appendChild(h("pre", { class: "mono", style: { whiteSpace: "pre-wrap", margin: "6px 0" } }, code));
    } else if (part) {
      wrap.appendChild(document.createTextNode(part));
    }
  });
  return wrap;
}

export function collapsible(title, content, { open = false } = {}) {
  const details = h("details", { class: "message-row", open });
  const summary = h("summary", {}, h("strong", { text: title }));
  const body = h("div", { class: "body" }, content);
  details.appendChild(summary);
  details.appendChild(body);
  return details;
}
