// 公共工具：DOM、图标、加载状态、通知、确认框、全屏面板与灯箱。
// 用户文本一律通过 textContent 写入；innerHTML 只用于下方静态图标常量。

export const bridge = window.AstrBotPluginView;

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const ICONS = {
  quote: '<path d="M7 7h4v4c0 3-1.5 5-4 6"/><path d="M14 7h4v4c0 3-1.5 5-4 6"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4.9a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.5a7 7 0 0 0-2 1.2l-2.4-.9-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-.9a7 7 0 0 0 2 1.2L10 21h4l.5-2.5a7 7 0 0 0 2-1.2l2.4.9 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>',
  backup: '<path d="M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z"/><path d="M4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.9-3"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.9 3"/><path d="M20 20v-4h-4"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  left: '<path d="m15 6-6 6 6 6"/>',
  right: '<path d="m9 6 6 6-6 6"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4h6v3"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
  bolt: '<path d="M13 3 5 14h6l-1 7 8-11h-6z"/>',
  download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  upload: '<path d="M12 20V9"/><path d="m7 14 5-5 5 5"/><path d="M5 4h14"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  broom: '<path d="M14 4 9 13"/><path d="M5 14h8l1 7H4z"/><path d="M8 17v4M11 17v4"/>',
  maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  shield: '<path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6z"/>',
  alert: '<path d="M12 3 2.8 19h18.4z"/><path d="M12 9v4M12 17h.01"/>',
};

/** 创建 18px 线性图标；装饰性图标对读屏隐藏。 */
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "i");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || ICONS.info;
  return svg;
}

export function hydrate(root = document) {
  $$("[data-i]", root).forEach((node) => {
    if (!node.querySelector(":scope > svg.i")) node.prepend(icon(node.dataset.i));
  });
}

/** el("div.card.side", {attrs}, ...children)；字符串子节点按纯文本写入。 */
export function el(spec, attrs = {}, ...children) {
  const [tag, ...classes] = spec.split(".");
  const node = document.createElement(tag || "div");
  if (classes.length) node.className = classes.join(" ");
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false || key === "icon") continue;
    if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "style") node.style.cssText = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  if (attrs?.icon) node.prepend(icon(attrs.icon));
  return node;
}

export function formatBytes(value) {
  let number = Number(value) || 0;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (number < 1024 || unit === "GB") return `${number.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    number /= 1024;
  }
  return "";
}

export function formatTime(value, withSeconds = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n) => String(n).padStart(2, "0");
  const base = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return withSeconds ? `${base}:${pad(date.getSeconds())}` : base;
}

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// ── 加载反馈 ─────────────────────────────────

let loading = 0;
/** 页面级请求：顶部 2px 进度线，重叠任务计数。 */
export async function task(callback) {
  loading += 1;
  $("#topline").classList.add("on");
  try {
    return await callback();
  } finally {
    loading = Math.max(0, loading - 1);
    if (!loading) $("#topline").classList.remove("on");
  }
}

/** 按钮触发的请求：按钮内转圈 + 进行时文案，执行中禁用防重复提交。 */
export async function busy(button, label, callback) {
  if (button.disabled) return undefined;
  const children = [...button.childNodes];
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.replaceChildren(el("span.spin", { "aria-hidden": "true" }), label);
  try {
    return await task(callback);
  } finally {
    button.replaceChildren(...children);
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

// ── 分段刻度条与数字 ─────────────────────────

export function segBar(segments, ratio, tone = "") {
  const bar = el(`div.seg${tone ? `.${tone}` : ""}`, { "aria-hidden": "true" });
  const on = Math.max(0, Math.min(segments, Math.round(segments * ratio)));
  for (let index = 0; index < segments; index += 1) {
    const cell = el("i");
    cell.style.setProperty("--k", index);
    bar.append(cell);
  }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    [...bar.children].forEach((cell, index) => cell.classList.toggle("on", index < on));
  }));
  return bar;
}

export function countUp(node, value, format = (n) => Math.round(n).toLocaleString("zh-CN")) {
  if (reducedMotion()) {
    node.textContent = format(value);
    return;
  }
  const start = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - start) / 500);
    node.textContent = format(value * (1 - (1 - progress) ** 3));
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// ── 通知 ─────────────────────────────────────

export function toast(message, kind = "ok", action) {
  const node = el("div.toast", { role: kind === "bad" ? "alert" : "status" },
    el(`span.${kind}`, {}, icon(kind === "bad" ? "alert" : kind === "ok" ? "check" : "info")),
    el("span", { text: message }));
  if (action) {
    node.append(el("button", { type: "button", text: action.label, onclick: () => { action.run(); close(); } }), el("span.timer"));
  }
  $("#toasts").append(node);
  function close() {
    node.classList.add("out");
    setTimeout(() => node.remove(), 200);
  }
  setTimeout(close, action ? 5000 : kind === "bad" ? 5000 : 2600);
}

// ── 焦点约束 ─────────────────────────────────

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]';

function trapFocus(container, event) {
  const nodes = $$(FOCUSABLE, container).filter((node) => node.getClientRects().length);
  if (!nodes.length) return;
  const first = nodes[0];
  const last = nodes.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

// ── 确认框 ───────────────────────────────────

/** 不可恢复操作的确认；确认按钮写明具体结果。返回 Promise<boolean>。 */
export function confirmAction({ title, message, confirm, danger = true }) {
  const dialog = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  const ok = $("#confirm-ok");
  ok.textContent = confirm;
  ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  const opener = document.activeElement;
  dialog.showModal();
  $("#confirm-cancel").focus();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "confirm");
      opener?.focus?.();
    }, { once: true });
  });
}

// ── 全屏面板 ─────────────────────────────────

let sheetOpener = null;
export function openSheet(title, ...content) {
  sheetOpener = document.activeElement;
  $("#sheet-title").textContent = title;
  $("#sheet-body").replaceChildren(...content);
  $("#scrim").classList.add("on");
  const sheet = $("#sheet");
  sheet.classList.add("on");
  sheet.setAttribute("aria-hidden", "false");
  sheet.inert = false;
  $("#sheet-close").focus();
}

export function closeSheet() {
  const sheet = $("#sheet");
  if (!sheet.classList.contains("on")) return;
  sheet.classList.remove("on");
  sheet.setAttribute("aria-hidden", "true");
  sheet.inert = true;
  $("#scrim").classList.remove("on");
  sheetOpener?.focus?.();
}

export const sheetOpen = () => $("#sheet").classList.contains("on");

// ── 灯箱：从缩略图位置放大（FLIP） ───────────

let lightboxItems = [];
let lightboxIndex = 0;

function placeImage(img, rect) {
  Object.assign(img.style, {
    left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`,
  });
}

function bigRect(img) {
  const ratio = img.naturalWidth && img.naturalHeight ? img.naturalWidth / img.naturalHeight : 1;
  const maxW = innerWidth * 0.8;
  const maxH = innerHeight * 0.8 - 60;
  const width = Math.min(maxW, maxH * ratio);
  const height = width / ratio;
  return { left: (innerWidth - width) / 2, top: (innerHeight - height) / 2 - 24, width, height };
}

/** buttons：同一组缩略图按钮（含 img）；index：起始项。 */
export function openLightbox(buttons, index) {
  lightboxItems = buttons;
  lightboxIndex = index;
  const box = $("#lightbox");
  const img = $("#lb-img");
  const thumb = buttons[index];
  img.style.transition = "none";
  placeImage(img, thumb.getBoundingClientRect());
  img.src = $("img", thumb).src;
  img.alt = $("img", thumb).alt;
  img.style.visibility = "visible";
  box.classList.add("on");
  box.setAttribute("aria-hidden", "false");
  box.inert = false;
  const grow = () => requestAnimationFrame(() => requestAnimationFrame(() => {
    img.style.transition = "";
    placeImage(img, bigRect(img));
  }));
  if (img.complete) grow(); else img.onload = grow;
  $("#lb-index").textContent = `${index + 1} / ${buttons.length}`;
  $("#lb-close").focus();
}

export function closeLightbox() {
  const box = $("#lightbox");
  if (!box.classList.contains("on")) return;
  const img = $("#lb-img");
  const thumb = lightboxItems[lightboxIndex];
  if (thumb?.isConnected) placeImage(img, thumb.getBoundingClientRect());
  box.classList.remove("on");
  box.setAttribute("aria-hidden", "true");
  box.inert = true;
  setTimeout(() => { if (!box.classList.contains("on")) img.style.visibility = "hidden"; }, 330);
  thumb?.focus?.();
}

function stepLightbox(delta) {
  if (!lightboxItems.length) return;
  lightboxIndex = (lightboxIndex + delta + lightboxItems.length) % lightboxItems.length;
  const img = $("#lb-img");
  img.src = $("img", lightboxItems[lightboxIndex]).src;
  img.onload = () => placeImage(img, bigRect(img));
  if (!reducedMotion()) {
    img.animate([{ opacity: 0, transform: `translateX(${delta * 16}px)` }, { opacity: 1, transform: "none" }],
      { duration: 200, easing: "cubic-bezier(.2,.8,.2,1)" });
  }
  $("#lb-index").textContent = `${lightboxIndex + 1} / ${lightboxItems.length}`;
}

export const lightboxOpen = () => $("#lightbox").classList.contains("on");

export function installOverlays() {
  $("#sheet-close").addEventListener("click", closeSheet);
  $("#scrim").addEventListener("click", closeSheet);
  $("#lb-close").addEventListener("click", closeLightbox);
  $("#lb-bg").addEventListener("click", closeLightbox);
  $("#lb-prev").addEventListener("click", () => stepLightbox(-1));
  $("#lb-next").addEventListener("click", () => stepLightbox(1));
  window.addEventListener("resize", () => {
    if (lightboxOpen()) placeImage($("#lb-img"), bigRect($("#lb-img")));
  });
  document.addEventListener("keydown", (event) => {
    if ($("#confirm").open) return;
    if (lightboxOpen()) {
      if (event.key === "Escape") closeLightbox();
      else if (event.key === "ArrowRight") stepLightbox(1);
      else if (event.key === "ArrowLeft") stepLightbox(-1);
      else if (event.key === "Tab") trapFocus($("#lightbox"), event);
      return;
    }
    if (sheetOpen()) {
      if (event.key === "Escape") closeSheet();
      else if (event.key === "Tab") trapFocus($("#sheet"), event);
    }
  });
}
