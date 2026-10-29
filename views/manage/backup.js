// 备份模块（导出、分步导入、完整性检查、头像缓存）与审计模块。

import { $, bridge, busy, confirmAction, el, formatBytes, formatTime, hydrate, toast } from "./core.js";

const STEPS = ["上传", "预检", "确认影响", "写入"];
const SOURCES = { command: "群聊指令", page: "管理页", page_node: "管理页 · 节点" };
const history = [];
const importState = { file: null, inspection: null, step: 0 };
const auditState = { items: [], loaded: false, error: "" };
let refreshAll = async () => {};

function log(message, ok = true) {
  history.unshift({ message, ok, at: new Date().toISOString() });
  history.splice(8);
  const list = $("#backup-log");
  list.replaceChildren(...(history.length
    ? history.map((item) => el("li", { class: item.ok ? "ok" : "bad" },
      el("span", { text: item.message }), el("small", { text: formatTime(item.at, true) })))
    : [el("li", {}, el("span.muted", { text: "本次打开页面后的操作会记录在这里。" }))]));
}

// ── 导出 ─────────────────────────────────────

async function exportBackup(button) {
  await busy(button, "正在打包", async () => {
    try {
      await bridge.download("backup/export", {}, "iconic-quotes-backup.zip");
      log("已导出备份");
      toast("备份已生成并开始下载");
    } catch (error) {
      log(`导出失败：${error.message}`, false);
      toast(`导出失败：${error.message}`, "bad");
    }
  });
}

// ── 导入：上传 → 预检 → 确认影响 → 写入 ─────

function renderSteps() {
  $("#import-steps").replaceChildren(...STEPS.map((label, index) => el("div", {
    class: index < importState.step ? "done" : index === importState.step ? "now" : "",
  }, el("i"), label)));
}

function renderInspection() {
  const box = $("#import-inspection");
  const result = importState.inspection;
  const commit = $("#import-commit");
  if (!result) {
    box.hidden = true;
    commit.hidden = true;
    return;
  }
  box.hidden = false;
  box.replaceChildren(el("dl.kv", {},
    el("dt", { text: "新增" }), el("dd", { text: `${result.added} 条` }),
    el("dt", { text: "重复跳过" }), el("dd", { text: `${result.duplicates} 条` }),
    el("dt", { text: "ID 冲突" }), el("dd", { text: `${result.conflicts} 条` }),
    el("dt", { text: "新增图片" }), el("dd", { text: formatBytes(result.image_bytes) }),
    el("dt", { text: "缺失图片" }), el("dd", {}, el(`span.chip.${result.missing_images ? "bad" : "ok"}`, { text: result.missing_images ? `${result.missing_images} 张` : "无" })),
    el("dt", { text: "可迁移配置" }), el("dd", { text: Object.keys(result.settings || {}).length ? `包含，别名 ${result.alias_count || 0} 个` : "无" })));
  commit.hidden = Boolean(result.missing_images) || !result.token;
  commit.textContent = result.added ? `确认导入 ${result.added} 条` : "确认导入（无新增记录）";
}

function resetImport() {
  importState.file = null;
  importState.inspection = null;
  importState.step = 0;
  $("#import-file").value = "";
  $("#file-name").textContent = "最大 1 GB，仅支持本插件导出的 ZIP（含 1.x 旧版）";
  $("#import-inspect").disabled = true;
  renderSteps();
  renderInspection();
}

function chooseFile(file) {
  if (!file) return;
  if (!/\.zip$/i.test(file.name)) {
    toast("请选择 ZIP 备份文件", "bad");
    return;
  }
  importState.file = file;
  importState.inspection = null;
  importState.step = 0;
  $("#file-name").textContent = `${file.name} · ${formatBytes(file.size)}`;
  $("#import-inspect").disabled = false;
  renderSteps();
  renderInspection();
}

async function inspect(button) {
  if (!importState.file) return;
  await busy(button, "预检中", async () => {
    try {
      importState.step = 1;
      renderSteps();
      const result = await bridge.upload("backup/import", importState.file);
      importState.inspection = result;
      importState.step = result.missing_images ? 1 : 2;
      renderSteps();
      renderInspection();
      log(result.missing_images ? `预检失败：缺少 ${result.missing_images} 张图片` : `预检通过：可新增 ${result.added} 条`, !result.missing_images);
    } catch (error) {
      importState.step = 0;
      renderSteps();
      log(`预检失败：${error.message}`, false);
      toast(`预检失败：${error.message}`, "bad");
    }
  });
}

async function commitImport(button) {
  const result = importState.inspection;
  if (!result?.token) return;
  const restore = $("#restore-settings").getAttribute("aria-checked") === "true";
  const ok = await confirmAction({
    title: "确认导入",
    message: `将新增 ${result.added} 条记录，跳过 ${result.duplicates} 条重复记录${restore ? "，并用备份中的可迁移配置覆盖当前配置" : ""}。现有记录不会被修改。`,
    confirm: `确认导入 ${result.added} 条`,
    danger: false,
  });
  if (!ok) return;
  await busy(button, "写入中", async () => {
    try {
      importState.step = 3;
      renderSteps();
      const done = await bridge.apiPost("backup/import/commit", { token: result.token, restore_settings: restore });
      const skipped = done.aliases_skipped ? `，未恢复 ${done.aliases_skipped} 个作者别名` : "";
      if (done.settings_restore_failed) {
        log("记录已导入，配置恢复失败", false);
        toast(done.settings_warning, "bad");
      } else {
        log(`导入完成：新增 ${done.added} 条`);
        toast(`导入完成：新增 ${done.added} 条，跳过重复 ${done.duplicates} 条${skipped}`);
      }
      resetImport();
      await refreshAll();
    } catch (error) {
      importState.step = 2;
      renderSteps();
      log(`导入失败：${error.message}`, false);
      toast(`导入失败：${error.message}。当前数据未被修改，请重新上传。`, "bad");
      resetImport();
    }
  });
}

// ── 完整性检查与头像 ─────────────────────────

async function integrity(button) {
  await busy(button, "检查中", async () => {
    try {
      const result = await bridge.apiPost("integrity/check", {});
      $("#integrity-result").replaceChildren(el("dl.kv", {},
        el("dt", { text: "已检查" }), el("dd", { text: `${result.checked} 个文件` }),
        el("dt", { text: "缺失或损坏" }), el("dd", {}, el(`span.chip.${result.missing ? "bad" : "ok"}`, { text: result.missing ? `${result.missing} 个` : "无" })),
        el("dt", { text: "已恢复" }), el("dd", { text: `${result.restored} 个` }),
        el("dt", { text: "受影响记录" }), el("dd", { text: `${result.broken_records} 条` })));
      log(`完整性检查：${result.checked} 个文件，异常 ${result.missing} 个`, !result.missing);
      toast(result.missing ? `发现 ${result.missing} 个缺失或损坏的媒体文件` : "全部媒体文件完好", result.missing ? "bad" : "ok");
      await refreshAll();
    } catch (error) {
      toast(`检查失败：${error.message}`, "bad");
    }
  });
}

export async function loadAvatarStats() {
  const stats = await bridge.apiGet("avatars/stats");
  $("#avatar-stats").textContent = `${stats.count} 个 · ${formatBytes(stats.bytes)}`;
}

async function cleanupAvatars(button, onCleared) {
  await busy(button, "清理中", async () => {
    try {
      const result = await bridge.apiPost("avatars/cleanup", {});
      onCleared();
      log(`清理无引用头像 ${result.deleted} 个`);
      toast(`已清理 ${result.deleted} 个无引用头像，释放 ${formatBytes(result.freed_bytes)}`);
      await loadAvatarStats();
    } catch (error) {
      toast(`清理失败：${error.message}`, "bad");
    }
  });
}

async function clearAvatars(button, onCleared) {
  const ok = await confirmAction({
    title: "清空头像缓存",
    message: "将删除全部本地头像文件。群典记录和图片不受影响，头像会在之后使用时重新下载。",
    confirm: "清空头像缓存",
  });
  if (!ok) return;
  await busy(button, "清空中", async () => {
    try {
      const result = await bridge.apiPost("avatars/clear", {});
      onCleared();
      log(`清空头像缓存 ${result.deleted} 个`);
      toast(`已清空 ${result.deleted} 个头像，释放 ${formatBytes(result.freed_bytes)}`);
      await loadAvatarStats();
    } catch (error) {
      toast(`清空失败：${error.message}`, "bad");
    }
  });
}

export function initBackup({ refresh, onAvatarsCleared }) {
  refreshAll = refresh;
  $("#export").addEventListener("click", (event) => exportBackup(event.currentTarget));
  $("#import-file").addEventListener("change", () => chooseFile($("#import-file").files[0]));
  const drop = $("#drop");
  drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    chooseFile(event.dataTransfer.files[0]);
  });
  $("#restore-settings").addEventListener("click", (event) => {
    const button = event.currentTarget;
    const on = button.getAttribute("aria-checked") !== "true";
    button.setAttribute("aria-checked", String(on));
    button.classList.toggle("on", on);
  });
  $("#import-inspect").addEventListener("click", (event) => inspect(event.currentTarget));
  $("#import-commit").addEventListener("click", (event) => commitImport(event.currentTarget));
  $("#integrity").addEventListener("click", (event) => integrity(event.currentTarget));
  $("#avatars-cleanup").addEventListener("click", (event) => cleanupAvatars(event.currentTarget, onAvatarsCleared));
  $("#avatars-clear").addEventListener("click", (event) => clearAvatars(event.currentTarget, onAvatarsCleared));
  resetImport();
  log("页面已打开");
  hydrate($("#view-backup"));
}

export const runIntegrity = () => $("#integrity").click();
export const runExport = () => $("#export").click();

// ── 审计 ─────────────────────────────────────

function renderAuditSide() {
  const counts = {};
  auditState.items.forEach((item) => { counts[item.source] = (counts[item.source] || 0) + 1; });
  $("#audit-side").replaceChildren(el("div.side-body", {}, el("h3", { text: "删除来源" }),
    el("dl.kv", {}, Object.entries(SOURCES).flatMap(([key, label]) => [el("dt", { text: label }), el("dd", { text: `${counts[key] || 0} 条` })])),
    el("p.muted", { text: "审计只保存记录 ID 与内容哈希，不保存被删正文；保留条数由“删除审计保留条数”控制。" })));
}

function renderAudit() {
  renderAuditSide();
  const group = $("#audit-group").value.trim();
  const operator = $("#audit-operator").value.trim().toLowerCase();
  const source = $("#audit-source").value;
  const rows = auditState.items.filter((item) =>
    (!group || item.group_id.includes(group))
    && (!operator || String(item.deleted_by).toLowerCase().includes(operator))
    && (!source || item.source === source));
  const body = $("#audit-rows");
  const cards = $("#audit-cards");
  let empty = null;
  if (auditState.error) empty = `加载失败：${auditState.error}`;
  else if (!auditState.items.length) empty = "还没有删除记录。";
  else if (!rows.length) empty = "没有符合筛选条件的审计记录。";
  $("#audit-total").textContent = `${rows.length} 条`;
  if (empty) {
    body.replaceChildren(el("tr.empty-row", {}, el("td", { colspan: "6" }, el("div.empty", { text: empty }))));
    cards.replaceChildren(el("div.card.empty", { text: empty }));
    return;
  }
  const scope = (item) => (item.node_path
    ? `${item.removed_nodes} 个节点${item.record_deleted ? "（记录已删空）" : ""}`
    : "整条记录");
  body.replaceChildren(...rows.map((item) => el("tr.static", {},
    el("td.mono", { text: formatTime(item.deleted_at, true) }),
    el("td.mono", { text: item.group_id }),
    el("td.mono", { text: item.record_id.slice(0, 8) }),
    el("td", { text: item.deleted_by }),
    el("td", {}, el("span.chip.plain", { text: SOURCES[item.source] || item.source })),
    el("td", { text: scope(item) }))));
  cards.replaceChildren(...rows.map((item) => el("article.card.m-card", {},
    el("div.top", {}, el("span.mono", { text: formatTime(item.deleted_at, true) }), el("span.chip.plain", { text: SOURCES[item.source] || item.source })),
    el("p", { text: `群 ${item.group_id} · 记录 ${item.record_id.slice(0, 8)} · ${scope(item)}` }),
    el("div.meta", {}, el("span", { text: item.deleted_by })))));
}

export async function loadAudit() {
  if (!auditState.loaded) {
    $("#audit-rows").replaceChildren(...Array.from({ length: 6 }, () => el("tr.sk-row", { "aria-hidden": "true" },
      ...Array.from({ length: 6 }, () => el("td", {}, el("span.sk"))))));
  }
  try {
    const data = await bridge.apiGet("audit", { limit: 1000 });
    auditState.items = data.items || [];
    auditState.error = "";
  } catch (error) {
    auditState.error = error.message;
  }
  auditState.loaded = true;
  renderAudit();
}

export function initAudit() {
  for (const id of ["#audit-group", "#audit-operator", "#audit-source"]) {
    $(id).addEventListener("input", renderAudit);
  }
  $("#audit-refresh").addEventListener("click", (event) => busy(event.currentTarget, "刷新中", loadAudit));
  hydrate($("#view-audit"));
}

