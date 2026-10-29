// 群典管理页入口：顶部悬浮岛导航、右下悬浮按钮（快捷菜单 / 草稿保存胶囊）与模块装配。

import { initAudit, initBackup, loadAudit, loadAvatarStats, runExport, runIntegrity } from "./backup.js";
import { changes, discard, initConfig, isDirty, loadConfig, save } from "./config.js";
import {
  $, $$, bridge, confirmAction, hydrate, installOverlays, lightboxOpen, reducedMotion, sheetOpen, task, toast,
} from "./core.js";
import { clearAvatarCache, focusSearch, initRecords, loadRecords, loadStats, recordGroups } from "./records.js";

let view = "records";

// ── 悬浮岛 ───────────────────────────────────

function movePill(instant = false) {
  const island = $("#island");
  const active = $("button.on", island);
  const pill = $(".pill", island);
  if (!active || !pill) return;
  if (instant || reducedMotion()) pill.style.transition = "none";
  pill.style.width = `${active.offsetWidth}px`;
  pill.style.transform = `translateX(${active.offsetLeft}px)`;
  if (instant) requestAnimationFrame(() => { pill.style.transition = ""; });
}

function showView(name) {
  view = name;
  $$("#island button").forEach((button) => {
    const on = button.dataset.view === name;
    button.classList.toggle("on", on);
    if (on) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  $$(".view").forEach((section) => section.classList.toggle("on", section.id === `view-${name}`));
  movePill();
  if (name === "audit") task(loadAudit);
  if (name === "backup") task(loadAvatarStats).catch(() => {});
}

// ── 悬浮按钮：快捷菜单与草稿胶囊 ─────────────

function setMenu(open) {
  const fab = $("#fab");
  const menu = $("#fab-menu");
  fab.setAttribute("aria-expanded", String(open));
  menu.classList.toggle("on", open);
  menu.inert = !open;
  if (open) $("button", menu).focus();
}

function onDraftChange(count) {
  const badge = $("#config-badge");
  badge.hidden = !count;
  badge.textContent = String(count);
  movePill();
  const pill = $("#pill");
  const fab = $("#fab");
  $("#pill-count").textContent = `${count} 项未保存`;
  if (count && !pill.classList.contains("on")) {
    setMenu(false);
    fab.hidden = true;
    pill.hidden = false;
    requestAnimationFrame(() => pill.classList.add("on"));
    if (!reducedMotion()) badge.animate([{ transform: "scale(.6)" }, { transform: "scale(1)" }], { duration: 200, easing: "cubic-bezier(.2,.8,.2,1)" });
  } else if (!count && pill.classList.contains("on")) {
    pill.classList.remove("on");
    $("#save-error").hidden = true;
    setTimeout(() => {
      if (!pill.classList.contains("on")) {
        pill.hidden = true;
        fab.hidden = false;
      }
    }, reducedMotion() ? 0 : 320);
  }
}

// ── 刷新 ─────────────────────────────────────

async function refreshAll() {
  await loadStats();
  await loadRecords();
}

async function refreshButton() {
  const count = changes().length;
  if (count && !await confirmAction({
    title: "放弃未保存的修改？",
    message: `刷新会重新载入已保存配置，并放弃 ${count} 项草稿修改。取消则保留草稿。`,
    confirm: "放弃并刷新",
  })) return;
  await task(async () => {
    try {
      await Promise.all([refreshAll(), loadConfig()]);
      if (view === "audit") await loadAudit();
      toast("数据已刷新");
    } catch (error) {
      toast(`刷新失败：${error.message}`, "bad");
    }
  });
}

const QUICK_ACTIONS = {
  refresh: refreshButton,
  search: () => { showView("records"); focusSearch(); },
  export: () => { showView("backup"); runExport(); },
  integrity: () => { showView("backup"); runIntegrity(); },
};

// ── 启动 ─────────────────────────────────────

async function main() {
  await bridge.ready();
  hydrate(document);
  installOverlays();
  initRecords();
  initConfig({ groups: recordGroups, onChange: onDraftChange });
  initBackup({ refresh: refreshAll, onAvatarsCleared: clearAvatarCache });
  initAudit();

  $("#island").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-view]");
    if (button) showView(button.dataset.view);
  });
  window.addEventListener("resize", () => movePill(true));
  $("#refresh").addEventListener("click", refreshButton);

  const fab = $("#fab");
  const menu = $("#fab-menu");
  fab.addEventListener("click", () => setMenu(fab.getAttribute("aria-expanded") !== "true"));
  menu.addEventListener("click", (event) => {
    const item = event.target.closest("[data-act]");
    if (!item) return;
    setMenu(false);
    fab.focus();
    QUICK_ACTIONS[item.dataset.act]();
  });
  menu.addEventListener("keydown", (event) => {
    const items = $$("button", menu);
    const index = items.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length].focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length].focus(); }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || $("#confirm").open || lightboxOpen() || sheetOpen()) return;
    if (fab.getAttribute("aria-expanded") === "true") {
      setMenu(false);
      fab.focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".fab-wrap") && fab.getAttribute("aria-expanded") === "true") setMenu(false);
  });
  $("#discard").addEventListener("click", discard);
  $("#save").addEventListener("click", (event) => save(event.currentTarget));

  requestAnimationFrame(() => movePill(true));
  await task(async () => {
    try {
      await Promise.all([refreshAll(), loadConfig()]);
    } catch (error) {
      $("#header-status").textContent = `连接失败：${error.message}`;
      toast(`加载失败：${error.message}`, "bad");
    }
  });
  if (isDirty()) onDraftChange(changes().length);
}

main();
