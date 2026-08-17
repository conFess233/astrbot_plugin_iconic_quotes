const bridge = window.AstrBotPluginPage;
await bridge.ready();

const state = { groups: [], page: 1, pageSize: 20, total: 0, selected: new Set(), config: null };
const $ = (selector) => document.querySelector(selector);

function notify(message, error = false) {
  const box = $("#notice");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.hidden = false;
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { box.hidden = true; }, 5000);
}

function formatBytes(value) {
  let number = Number(value || 0);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (number < 1024 || unit === "GB") return `${number.toFixed(1)} ${unit}`;
    number /= 1024;
  }
  return `${number.toFixed(1)} GB`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadStats() {
  const data = await bridge.apiGet("stats");
  state.groups = data.groups;
  const total = data.groups.reduce((sum, group) => sum + group.total, 0);
  const broken = data.groups.reduce((sum, group) => sum + group.broken, 0);
  const metrics = [
    ["群聊数量", data.groups.length],
    ["群典总数", total],
    ["异常记录", broken],
    ["媒体占用", formatBytes(data.media_bytes)],
  ];
  const summary = $("#summary");
  summary.replaceChildren(...metrics.map(([label, value]) => {
    const card = element("article", "metric");
    card.append(element("span", "", label), element("strong", "", String(value)));
    return card;
  }));
  const select = $("#group");
  const previous = select.value;
  select.replaceChildren(...data.groups.map((group) => {
    const option = element("option", "", `${group.group_id} · ${group.total} 条`);
    option.value = group.group_id;
    return option;
  }));
  if (data.groups.some((group) => group.group_id === previous)) select.value = previous;
  if (data.groups.length) await loadRecords();
  else $("#records").replaceChildren(element("p", "", "暂无群典数据。"));
}

async function loadRecords() {
  const groupId = $("#group").value;
  if (!groupId) return;
  const data = await bridge.apiGet("records", {
    group_id: groupId,
    page: state.page,
    page_size: state.pageSize,
    q: $("#search").value,
    type: $("#type").value,
    has_image: $("#has-image").value,
    health: $("#health").value,
  });
  state.total = data.total;
  state.selected.clear();
  $("#delete-selected").disabled = true;
  $("#record-total").textContent = `${data.total} 条记录`;
  $("#page-label").textContent = `第 ${data.page} 页`;
  $("#previous").disabled = data.page <= 1;
  $("#next").disabled = data.page * data.page_size >= data.total;
  const container = $("#records");
  container.replaceChildren(...data.items.map(renderRecord));
  if (!data.items.length) container.append(element("p", "", "没有符合筛选条件的记录。"));
}

function renderRecord(record) {
  const article = element("article", "record");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.addEventListener("change", () => {
    checkbox.checked ? state.selected.add(record.id) : state.selected.delete(record.id);
    $("#delete-selected").disabled = state.selected.size === 0;
  });
  const body = element("div");
  const head = element("div", "record-head");
  const author = record.type === "message"
    ? (record.author?.nickname || record.author?.user_id || "未知用户")
    : `合并转发 · ${record.nodes.length} 个节点`;
  head.append(
    element("div", "record-title", `${author}${record.broken ? " · 异常记录" : ""}`),
    element("div", "record-meta", `${record.recorded_at} · ${record.id}`),
  );
  body.append(head);
  if (record.type === "message") {
    appendSegments(body, record.segments);
    const preview = element("button", "button ghost", "预览卡片");
    preview.addEventListener("click", () => previewCard(record));
    body.append(preview);
  } else {
    record.nodes.forEach((node) => {
      const nodeElement = element("section", "node");
      nodeElement.append(element("strong", "", node.author.nickname || node.author.user_id || "身份不完整"));
      appendSegments(nodeElement, node.segments);
      body.append(nodeElement);
    });
  }
  article.append(checkbox, body);
  return article;
}

function appendSegments(parent, segments) {
  const text = segments.filter((segment) => segment.type === "text").map((segment) => segment.text || "").join("");
  if (text) parent.append(element("div", "quote-text", text));
  const images = segments.filter((segment) => segment.type === "image" && segment.path);
  if (!images.length) return;
  const grid = element("div", "media-grid");
  images.forEach(async (segment) => {
    const image = document.createElement("img");
    image.alt = "群典图片";
    grid.append(image);
    try {
      const result = await bridge.apiGet("media-data", { path: segment.path });
      image.src = result.data_url;
    } catch (error) {
      image.alt = `图片不可用：${error.message}`;
    }
  });
  parent.append(grid);
}

async function previewCard(record) {
  try {
    const data = await bridge.apiPost("preview", { group_id: record.group_id, record_id: record.id });
    const area = $("#preview-area");
    area.hidden = false;
    area.replaceChildren(element("h2", "", "卡片预览"));
    data.images.forEach((source) => {
      const image = document.createElement("img");
      image.src = source;
      image.alt = "卡片预览";
      area.append(image);
    });
    activateTab("config");
  } catch (error) { notify(error.message, true); }
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${name}-panel`));
  if (name === "audit") loadAudit().catch((error) => notify(error.message, true));
}

async function loadConfig() {
  state.config = await bridge.apiGet("config");
  $("#config-editor").value = JSON.stringify(state.config, null, 2);
  $("#migration-path").value = state.config.storage_subdir;
}

async function loadAudit() {
  const data = await bridge.apiGet("audit", { limit: 300 });
  $("#audit").replaceChildren(...data.items.slice().reverse().map((item) =>
    element("div", "audit-row", `${item.deleted_at} · 群 ${item.group_id} · ${item.record_id} · ${item.deleted_by} · ${item.source}`),
  ));
}

function confirmAction(title, message) {
  const dialog = $("#confirm-dialog");
  $("#dialog-title").textContent = title;
  $("#dialog-message").textContent = message;
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
$("#refresh").addEventListener("click", () => Promise.all([loadStats(), loadConfig()]).catch((error) => notify(error.message, true)));
$("#search-button").addEventListener("click", () => { state.page = 1; loadRecords().catch((error) => notify(error.message, true)); });
$("#group").addEventListener("change", () => { state.page = 1; loadRecords().catch((error) => notify(error.message, true)); });
$("#previous").addEventListener("click", () => { state.page -= 1; loadRecords().catch((error) => notify(error.message, true)); });
$("#next").addEventListener("click", () => { state.page += 1; loadRecords().catch((error) => notify(error.message, true)); });

$("#delete-selected").addEventListener("click", async () => {
  if (!await confirmAction("删除群典", `确定永久删除所选 ${state.selected.size} 条记录吗？`)) return;
  try {
    const result = await bridge.apiPost("records/delete", { group_id: $("#group").value, record_ids: [...state.selected] });
    notify(`已删除 ${result.deleted} 条记录。`);
    await loadStats();
  } catch (error) { notify(error.message, true); }
});

$("#save-config").addEventListener("click", async () => {
  try {
    const value = JSON.parse($("#config-editor").value);
    state.config = await bridge.apiPost("config/save", value);
    $("#config-editor").value = JSON.stringify(state.config, null, 2);
    notify("配置已保存并热更新。" );
  } catch (error) { notify(error.message, true); }
});
$("#reload-config").addEventListener("click", () => loadConfig().catch((error) => notify(error.message, true)));
$("#reset-card-style").addEventListener("click", () => {
  try {
    const value = JSON.parse($("#config-editor").value);
    value.card_width = 1200;
    value.card_min_height = 480;
    value.card_max_height = 2000;
    value.card_custom_css = "";
    $("#config-editor").value = JSON.stringify(value, null, 2);
    notify("已在编辑器中恢复默认卡片样式；请点击“校验并保存”生效。");
  } catch (error) { notify(`配置 JSON 无效：${error.message}`, true); }
});
$("#export").addEventListener("click", () => bridge.download("backup/export", {}, "iconic-quotes-backup.zip").catch((error) => notify(error.message, true)));

$("#import").addEventListener("click", async () => {
  const file = $("#import-file").files[0];
  if (!file) return notify("请先选择 ZIP 备份。", true);
  try {
    const inspection = await bridge.upload("backup/import", file);
    if (inspection.missing_images) {
      return notify(`预检失败：缺少 ${inspection.missing_images} 张被记录引用的图片。`, true);
    }
    const summary = [
      `将新增 ${inspection.added} 条`,
      `重复 ${inspection.duplicates} 条`,
      `ID 冲突 ${inspection.conflicts} 条（跳过）`,
      `新增图片 ${formatBytes(inspection.image_bytes)}`,
    ].join("；");
    if (!await confirmAction("确认导入备份", `${summary}。确认执行合并吗？`)) return;
    const result = await bridge.apiPost("backup/import/commit", {
      token: inspection.token,
      restore_settings: $("#restore-settings").checked,
    });
    notify(`导入完成：新增 ${result.added} 条，跳过重复 ${result.duplicates} 条。`);
    await Promise.all([loadStats(), loadConfig()]);
  } catch (error) { notify(error.message, true); }
});

$("#migrate").addEventListener("click", async () => {
  const target = $("#migration-path").value.trim();
  if (!target) return notify("请输入目标相对目录。", true);
  if (!await confirmAction("迁移存储", `确定将数据迁移到 ${target} 吗？`)) return;
  try {
    const result = await bridge.apiPost("storage/migrate", { storage_subdir: target });
    notify(`迁移完成。旧目录备份：${result.backup_root}`);
    await loadConfig();
  } catch (error) { notify(error.message, true); }
});

try {
  await Promise.all([loadStats(), loadConfig()]);
} catch (error) {
  notify(error.message, true);
}
