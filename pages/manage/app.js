const bridge = window.AstrBotPluginPage;
await bridge.ready();

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  groups: [], records: [], audit: [], page: 1,
  pageSize: Number(sessionStorage.getItem("iconic-quotes-page-size") || 20),
  total: 0, selected: new Set(), config: null, draft: null,
  dirty: false, activeRecord: null, overrideGroup: "",
  media: new Map(), lightboxItems: [], lightboxIndex: 0, loading: 0,
};

const ICONS = {
  archive: '<path d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6"/>',
  alert: '<path d="M12 3 2.8 19h18.4zM12 9v4M12 17h.01"/>',
  backup: '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  chevronDown: '<path d="m8 10 4 4 4-4"/>', chevronLeft: '<path d="m15 18-6-6 6-6"/>', chevronRight: '<path d="m9 18 6-6-6-6"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>', delete: '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/>',
  download: '<path d="M12 3v12m0 0 5-5m-5 5-5-5M5 20h14"/>', filter: '<path d="M4 5h16l-6 7v6l-4 2v-8z"/>',
  folder: '<path d="M3 6h7l2 2h9v11H3z"/>', groups: '<path d="M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 20v-2a4 4 0 0 0-3-3.8M16 2.2a4 4 0 0 1 0 7.6"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2"/>', image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m4 17 5-5 4 4 2-2 5 5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>', more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  refresh: '<path d="M20 6v5h-5M4 18v-5h5M18.7 9A7 7 0 0 0 6.6 5.6L4 8M5.3 15A7 7 0 0 0 17.4 18.4L20 16"/>', save: '<path d="M5 3h12l2 2v16H5zM8 3v6h8V3M8 21v-7h8v7"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>', settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H3v-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V3h4v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"/>',
  upload: '<path d="M12 16V4m0 0L7 9m5-5 5 5M5 20h14"/>', quotes: '<path d="M7 17H4v-4a6 6 0 0 1 6-6v3a3 3 0 0 0-3 3zM17 17h-3v-4a6 6 0 0 1 6-6v3a3 3 0 0 0-3 3z"/>',
};

function icon(name, className = "") {
  const key = name.replace(/-([a-z])/g, (_, value) => value.toUpperCase());
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  if (className) svg.setAttribute("class", className);
  svg.innerHTML = ICONS[key] || ICONS.info;
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    if (!node.querySelector("svg")) node.prepend(icon(node.dataset.icon));
  });
  root.querySelectorAll("[data-icon-host]").forEach((node) => {
    if (!node.querySelector("svg")) node.append(icon(node.dataset.iconHost));
  });
}

function setLoading(active) {
  state.loading += active ? 1 : -1;
  state.loading = Math.max(0, state.loading);
  $("#progress").hidden = state.loading === 0;
}

async function task(callback) {
  setLoading(true);
  try { return await callback(); } finally { setLoading(false); }
}

function notify(message, error = false) {
  const toast = element("div", `toast${error ? " error" : ""}`);
  toast.append(icon(error ? "alert" : "info"), element("span", "", message));
  $("#toast-stack").append(toast);
  window.setTimeout(() => {
    toast.classList.add("out");
    window.setTimeout(() => toast.remove(), 240);
  }, 4200);
}

function formatBytes(value) {
  let number = Number(value || 0);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (number < 1024 || unit === "GB") return `${number.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    number /= 1024;
  }
  return `${number.toFixed(1)} GB`;
}

function dateParts(value) {
  const raw = String(value || "—");
  const parts = raw.replace("T", " ").split(" ");
  return [parts[0] || "—", parts.slice(1).join(" ").replace(/Z$/, "") || ""];
}

function authorOf(record) {
  if (record.type === "forward") return { nickname: "合并转发", user_id: `${record.nodes?.length || 0} 个节点` };
  return record.author || {};
}

function segmentsOf(record) {
  if (record.type === "message") return record.segments || [];
  return (record.nodes || []).flatMap((node) => node.segments || []);
}

function textOf(record) {
  if (record.type === "message") return (record.segments || []).filter((x) => x.type === "text").map((x) => x.text || "").join("") || "（仅图片消息）";
  return (record.nodes || []).map((node) => {
    const who = node.author?.nickname || node.author?.user_id || "未知发送者";
    const text = (node.segments || []).filter((x) => x.type === "text").map((x) => x.text || "").join("") || "（仅图片）";
    return `${who}：${text}`;
  }).join(" · ") || "（空合并转发）";
}

function imageSegments(record) { return segmentsOf(record).filter((x) => x.type === "image" && x.path); }

const ROLE_OPTIONS = [["bot_admin", "Bot 管理员"], ["owner", "群主"], ["admin", "群管理员"], ["member", "群成员"], ["everyone", "所有人"]];
const CONFIG_SECTIONS = [
  { id: "trigger-send", title: "触发与发送", description: "设置关键词、发送条数和输出形式。", icon: "quotes", fields: [
    { key: "add_keyword_enabled", label: "启用添加关键词", type: "boolean", hint: "关闭后仍可使用 /添加群典 命令。" },
    { key: "add_keywords", label: "添加关键词", type: "list", hint: "每行一个关键词。" },
    { key: "query_keyword_enabled", label: "启用查询关键词", type: "boolean", hint: "命令消息不会再次触发关键词。" },
    { key: "query_keywords", label: "查询关键词", type: "list", hint: "每行一个关键词。" },
    { key: "send_count", label: "最大发送条数", type: "number", min: 1, max: 10 },
    { key: "random_send_count", label: "随机发送条数", type: "boolean", hint: "开启后每次从 1 到最大发送条数中随机。" },
    { key: "send_mode", label: "单条发送方式", type: "select", options: [["text", "文字"], ["card", "图片卡片"]] },
    { key: "aggregate_multiple", label: "多条聚合为合并转发", type: "boolean" },
    { key: "allow_bot_authors", label: "允许记录 Bot 消息", type: "boolean" },
  ]},
  { id: "limits", title: "容量与限制", description: "控制本地存储和单条消息的安全边界。", icon: "archive", fields: [
    { key: "max_records_per_group", label: "单群记录上限", type: "number", min: 1, max: 100000 },
    { key: "max_media_mb", label: "媒体目录上限（MB）", type: "number", min: 1, max: 102400 },
    { key: "max_image_mb", label: "单图上限（MB）", type: "number", min: 1, max: 100 },
    { key: "max_images_per_record", label: "单条图片上限", type: "number", min: 1, max: 100 },
    { key: "max_forward_nodes", label: "转发节点上限", type: "number", min: 1, max: 200 },
    { key: "max_text_chars", label: "普通消息字符上限", type: "number", min: 1, max: 100000 },
    { key: "max_forward_text_chars", label: "转发消息字符上限", type: "number", min: 1, max: 1000000 },
    { key: "delete_preview_limit", label: "删除预览上限", type: "number", min: 1, max: 50 },
    { key: "audit_limit", label: "删除审计保留条数", type: "number", min: 1, max: 100000 },
  ]},
  { id: "permissions", title: "权限", description: "分别控制添加、查询、统计与删除操作。", icon: "groups", fields: [
    { key: "add_roles", label: "添加权限", type: "roles", full: true }, { key: "query_roles", label: "查询权限", type: "roles", full: true },
    { key: "info_roles", label: "查看统计权限", type: "roles", full: true }, { key: "delete_roles", label: "删除权限", type: "roles", full: true },
  ]},
  { id: "lists", title: "名单", description: "白名单为空时不限制；ID 每行一个。", icon: "filter", fields: [
    { key: "group_blacklist", label: "群黑名单", type: "list" }, { key: "group_whitelist", label: "群白名单", type: "list" },
    { key: "user_blacklist", label: "用户黑名单", type: "list" }, { key: "user_whitelist", label: "用户白名单", type: "list" },
    { key: "excluded_author_ids", label: "不记录的发送者 ID", type: "list", full: true },
  ]},
  { id: "card", title: "卡片样式", description: "控制图片卡片尺寸与自定义 CSS。", icon: "image", fields: [
    { key: "card_auto_height", label: "自动裁切卡片高度", type: "boolean", hint: "根据内容缩短画布，减少底部留白。" },
    { key: "card_width", label: "卡片宽度（px）", type: "number", min: 480, max: 3000 },
    { key: "card_min_height", label: "最小高度（px）", type: "number", min: 240, max: 3000 },
    { key: "card_max_height", label: "最大高度（px）", type: "number", min: 480, max: 6000 },
    { key: "card_custom_css", label: "自定义 CSS", type: "textarea", full: true, hint: "保存时会经过安全过滤。" },
  ], reset: true },
  { id: "cooldown", title: "冷却与重试", description: "控制插件全局冷却和发送失败重试。", icon: "refresh", fields: [
    { key: "global_cooldown_ms", label: "全局冷却（毫秒）", type: "number", min: 0, max: 60000 },
    { key: "cooldown_message", label: "冷却提示", type: "text" },
    { key: "send_retry_count", label: "发送重试次数", type: "number", min: 0, max: 5 },
    { key: "send_retry_delay_ms", label: "重试延迟（毫秒）", type: "number", min: 100, max: 10000 },
    { key: "retry_on_ambiguous_failure", label: "不明确失败时也重试", type: "boolean", hint: "可能造成重复发送，默认关闭。" },
  ]},
];

const ALL_FIELDS = CONFIG_SECTIONS.flatMap((section) => section.fields);
const OVERRIDE_KEYS = new Set(["add_keyword_enabled", "add_keywords", "query_keyword_enabled", "query_keywords", "send_count", "random_send_count", "send_mode", "aggregate_multiple", "allow_bot_authors", "max_records_per_group", "add_roles", "query_roles", "info_roles", "delete_roles", "user_blacklist", "user_whitelist", "excluded_author_ids"]);
const OVERRIDE_FIELDS = ALL_FIELDS.filter((field) => OVERRIDE_KEYS.has(field.key));

function normalizeList(value) { return Array.isArray(value) ? value : []; }
function parseList(value) { return [...new Set(value.split(/\r?\n|,/).map((x) => x.trim()).filter(Boolean))]; }

function renderControl(field, value, onChange, idPrefix = "global") {
  const id = `${idPrefix}-${field.key}`;
  if (field.type === "boolean") {
    const label = element("label", "switch-row");
    const input = document.createElement("input"); input.type = "checkbox"; input.id = id; input.checked = Boolean(value);
    input.addEventListener("change", () => onChange(input.checked));
    label.append(input, element("span", "switch"), element("span", "", field.hint || (input.checked ? "已启用" : "已停用")));
    return label;
  }
  if (field.type === "roles") {
    const grid = element("div", "role-grid");
    ROLE_OPTIONS.forEach(([role, name]) => {
      const label = element("label", "role-chip"); const input = document.createElement("input"); input.type = "checkbox"; input.value = role; input.checked = normalizeList(value).includes(role);
      input.addEventListener("change", () => onChange([...grid.querySelectorAll("input:checked")].map((item) => item.value)));
      label.append(input, document.createTextNode(name)); grid.append(label);
    }); return grid;
  }
  if (field.type === "select") {
    const select = document.createElement("select"); select.id = id;
    field.options.forEach(([optionValue, name]) => { const option = element("option", "", name); option.value = optionValue; option.selected = optionValue === value; select.append(option); });
    select.addEventListener("change", () => onChange(select.value)); return select;
  }
  const input = field.type === "textarea" || field.type === "list" ? document.createElement("textarea") : document.createElement("input");
  input.id = id;
  if (field.type === "number") { input.type = "number"; input.min = field.min; input.max = field.max; input.value = value ?? ""; }
  else if (field.type === "list") { input.rows = 4; input.value = normalizeList(value).join("\n"); }
  else { if (input.tagName === "INPUT") input.type = "text"; input.value = value ?? ""; if (field.type === "textarea") input.rows = 10; }
  input.addEventListener("input", () => onChange(field.type === "number" ? Number(input.value) : field.type === "list" ? parseList(input.value) : input.value));
  return input;
}

function syncEditor() { if (state.draft) $("#config-editor").value = JSON.stringify(state.draft, null, 2); }
function setDirty(value = true) { state.dirty = value; $("#dirty-bar").hidden = !value; }
function updateDraft(key, value) { state.draft[key] = value; setDirty(); syncEditor(); }

function renderConfig() {
  const nav = $("#config-nav"); const sections = $("#config-sections");
  nav.replaceChildren(); sections.replaceChildren();
  CONFIG_SECTIONS.forEach((section, index) => {
    const navButton = element("button", `btn btn-text${index === 0 ? " active" : ""}`, section.title); navButton.type = "button"; navButton.prepend(icon(section.icon));
    navButton.addEventListener("click", () => { $$("#config-nav button").forEach((button) => button.classList.remove("active")); navButton.classList.add("active"); $(`#config-${section.id}`).scrollIntoView({ behavior: "smooth", block: "start" }); }); nav.append(navButton);
    const card = element("section", "section-card config-section"); card.id = `config-${section.id}`;
    const titleRow = element("div", "section-title-row"); const heading = element("div"); heading.append(element("h2", "", section.title), element("p", "", section.description)); titleRow.append(heading);
    if (section.reset) { const reset = element("button", "btn btn-tonal", "恢复默认样式"); reset.type = "button"; reset.prepend(icon("refresh")); reset.addEventListener("click", resetCardStyle); titleRow.append(reset); }
    card.append(titleRow); const grid = element("div", "field-grid");
    section.fields.forEach((field) => { const wrapper = element("div", `field-card${field.full ? " full" : ""}`); wrapper.append(element("label", "", field.label), renderControl(field, state.draft[field.key], (value) => updateDraft(field.key, value))); if (field.hint && field.type !== "boolean") wrapper.append(element("small", "", field.hint)); grid.append(wrapper); });
    card.append(grid); sections.append(card);
  });
  renderOverrideFields(); syncEditor(); hydrateIcons(sections);
}

function resetCardStyle() {
  Object.assign(state.draft, { card_width: 1200, card_min_height: 480, card_max_height: 2000, card_custom_css: "", card_auto_height: true });
  setDirty(); renderConfig(); notify("已在草稿中恢复默认卡片样式。");
}

function chosenOverrideGroup() { return $("#override-group-input").value.trim() || $("#override-group-select").value; }
function openOverrideGroup() {
  const groupId = chosenOverrideGroup();
  if (!/^\d+$/.test(groupId)) return notify("群号只能包含数字。", true);
  state.overrideGroup = groupId; $("#override-group-input").value = groupId; $("#override-status").textContent = `正在编辑群 ${groupId}；未启用的字段继承全局值。`; renderOverrideFields();
}

function renderOverrideFields() {
  const container = $("#override-fields"); container.replaceChildren();
  if (!state.draft || !state.overrideGroup) return;
  const overrides = state.draft.group_overrides || (state.draft.group_overrides = {}); const current = overrides[state.overrideGroup] || {};
  OVERRIDE_FIELDS.forEach((field) => {
    const enabled = Object.hasOwn(current, field.key); const row = element("div", `override-row${enabled ? "" : " disabled"}`);
    const enableLabel = element("label", "override-enable"); const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = enabled; enableLabel.append(checkbox, document.createTextNode(field.label));
    const control = element("div", "override-control"); control.append(renderControl(field, enabled ? current[field.key] : state.draft[field.key], (value) => { current[field.key] = value; overrides[state.overrideGroup] = current; setDirty(); syncEditor(); }, `override-${state.overrideGroup}`));
    checkbox.addEventListener("change", () => { if (checkbox.checked) { current[field.key] = structuredClone(state.draft[field.key]); overrides[state.overrideGroup] = current; } else { delete current[field.key]; if (!Object.keys(current).length) delete overrides[state.overrideGroup]; } setDirty(); syncEditor(); renderOverrideFields(); });
    row.append(enableLabel, control); container.append(row);
  });
}

async function clearOverride() {
  if (!state.overrideGroup || !state.draft.group_overrides?.[state.overrideGroup]) return notify("当前群没有覆盖配置。", true);
  if (!await confirmAction("清空群聊覆盖", `确定清空群 ${state.overrideGroup} 的全部覆盖字段吗？`)) return;
  delete state.draft.group_overrides[state.overrideGroup]; setDirty(); syncEditor(); renderOverrideFields(); notify("已从草稿中清空群聊覆盖配置。");
}

function renderStats(data) {
  const total = data.groups.reduce((sum, group) => sum + Number(group.total || 0), 0); const broken = data.groups.reduce((sum, group) => sum + Number(group.broken || 0), 0);
  const metrics = [["群典总数", total, "quotes"], ["群聊数量", data.groups.length, "groups"], ["异常记录", broken, "alert"], ["媒体占用", formatBytes(data.media_bytes), "image"]];
  $("#summary").replaceChildren(...metrics.map(([label, value, iconName]) => { const card = element("article", "metric"); const iconBox = element("span", "metric-icon"); iconBox.append(icon(iconName)); const body = element("div"); body.append(element("span", "metric-label", label), element("strong", "metric-value", String(value))); card.append(iconBox, body); return card; }));
  $("#header-status").textContent = `已连接 · ${data.groups.length} 个群聊 · ${total} 条记录`;
}

function updateGroupOptions() {
  const previous = $("#group").value; const options = state.groups.map((group) => { const option = element("option", "", `${group.group_id} · ${group.total} 条`); option.value = group.group_id; return option; });
  if (!options.length) { const option = element("option", "", "暂无群聊"); option.value = ""; options.push(option); }
  $("#group").replaceChildren(...options); if (state.groups.some((group) => group.group_id === previous)) $("#group").value = previous;
  const groupOptions = [element("option", "", "选择已有群聊")]; groupOptions[0].value = "";
  state.groups.forEach((group) => { const option = element("option", "", group.group_id); option.value = group.group_id; groupOptions.push(option); }); $("#override-group-select").replaceChildren(...groupOptions);
}

async function loadStats() {
  const data = await bridge.apiGet("stats"); state.groups = data.groups || []; state.storageRoot = data.storage_root || ""; renderStats(data); updateGroupOptions();
  if (state.groups.length) await loadRecords(); else renderEmptyRecords("暂无群典数据");
}

function currentGroupInfo() { return state.groups.find((group) => group.group_id === $("#group").value); }
function updateGroupSummary() { const group = currentGroupInfo(); $("#group-summary").textContent = group ? `群 ${group.group_id} · ${group.total} / ${group.limit} 条 · 普通消息 ${group.messages} · 合并转发 ${group.forwards} · 含图 ${group.with_images}` : "选择群聊以查看记录"; }

function renderSkeletonRows() {
  const rows = Array.from({ length: 6 }, () => { const tr = document.createElement("tr"); for (let index = 0; index < 8; index += 1) { const td = document.createElement("td"); const bar = element("div", "skeleton"); bar.style.width = `${45 + (index * 13) % 45}%`; td.append(bar); tr.append(td); } return tr; }); $("#record-rows").replaceChildren(...rows);
}

async function loadRecords() {
  const groupId = $("#group").value; if (!groupId) return;
  renderSkeletonRows(); updateGroupSummary();
  const data = await bridge.apiGet("records", { group_id: groupId, page: state.page, page_size: state.pageSize, q: $("#search").value, type: $("#type").value, has_image: $("#has-image").value, health: $("#health").value });
  state.records = data.items || []; state.total = data.total || 0; state.page = data.page || state.page; renderRecords();
}

function renderEmptyRecords(message) { const td = element("td", "empty-cell", message); td.colSpan = 8; const tr = document.createElement("tr"); tr.append(td); $("#record-rows").replaceChildren(tr); $("#record-cards").replaceChildren(element("div", "record-card", message)); }

function selectionChanged() {
  const count = state.selected.size; $("#selection-label").textContent = count ? `已选择 ${count} 条` : "未选择记录"; $("#delete-selected").disabled = count === 0;
  $("#select-page").checked = state.records.length > 0 && state.records.every((record) => state.selected.has(record.id));
  $$("[data-record-id]").forEach((node) => node.classList.toggle("selected", state.selected.has(node.dataset.recordId)));
  $$("input[data-select-id]").forEach((input) => { input.checked = state.selected.has(input.dataset.selectId); });
}

function recordCheckbox(record) { const input = document.createElement("input"); input.type = "checkbox"; input.dataset.selectId = record.id; input.setAttribute("aria-label", `选择记录 ${record.id}`); input.checked = state.selected.has(record.id); input.addEventListener("click", (event) => event.stopPropagation()); input.addEventListener("change", () => { if (input.checked && state.selected.size >= 100) { input.checked = false; notify("后台每次最多删除 100 条记录。", true); } else if (input.checked) state.selected.add(record.id); else state.selected.delete(record.id); selectionChanged(); }); return input; }

function makeChip(record) { return element("span", `type-chip${record.type === "forward" ? " forward" : ""}`, record.type === "forward" ? "合并转发" : "普通消息"); }
function makeStatus(record) { return element("span", `status-chip${record.broken ? " broken" : ""}`, record.broken ? "异常" : "正常"); }

function rowMenu(record) {
  const wrap = element("div", "row-menu"); const button = element("button", "icon-btn"); button.type = "button"; button.append(icon("more"));
  button.addEventListener("click", (event) => { event.stopPropagation(); $$(".row-menu-pop").forEach((menu) => { if (menu !== pop) menu.hidden = true; }); pop.hidden = !pop.hidden; });
  const pop = element("div", "row-menu-pop"); pop.hidden = true; const detail = element("button", "btn btn-text", "查看详情"); detail.type = "button"; detail.prepend(icon("info")); detail.addEventListener("click", (event) => { event.stopPropagation(); openDrawer(record); pop.hidden = true; }); const remove = element("button", "btn btn-text", "删除记录"); remove.type = "button"; remove.prepend(icon("delete")); remove.addEventListener("click", async (event) => { event.stopPropagation(); pop.hidden = true; await deleteRecords([record.id]); }); pop.append(detail, remove); wrap.append(button, pop); return wrap;
}

function renderRecordRow(record) {
  const tr = document.createElement("tr"); tr.dataset.recordId = record.id; tr.addEventListener("click", () => openDrawer(record));
  const check = element("td", "check-cell"); check.append(recordCheckbox(record)); const type = document.createElement("td"); type.append(makeChip(record));
  const author = authorOf(record); const authorTd = element("td", "author-cell"); authorTd.append(element("strong", "", author.nickname || "未知发送者"), element("small", "", author.user_id || "身份不完整"));
  const content = document.createElement("td"); content.append(element("div", "summary-text", textOf(record)));
  const images = element("td", "", String(imageSegments(record).length)); const [date, time] = dateParts(record.recorded_at); const timeTd = element("td", "time-cell"); timeTd.append(document.createTextNode(date), element("small", "", time)); const status = document.createElement("td"); status.append(makeStatus(record)); const menu = element("td", "menu-cell"); menu.append(rowMenu(record));
  tr.append(check, type, authorTd, content, images, timeTd, status, menu); return tr;
}

function renderRecordCard(record) {
  const card = element("article", "record-card"); card.dataset.recordId = record.id; card.addEventListener("click", () => openDrawer(record)); const head = element("div", "record-card-head"); const title = element("div", "record-card-title"); title.append(recordCheckbox(record), makeChip(record), element("strong", "", authorOf(record).nickname || "未知发送者")); head.append(title, rowMenu(record)); card.append(head, element("div", "summary-text", textOf(record))); const meta = element("div", "record-card-meta"); meta.append(element("span", "", `${imageSegments(record).length} 张图片 · ${dateParts(record.recorded_at).join(" ")}`), makeStatus(record)); card.append(meta); return card;
}

function renderRecords() {
  if (!state.records.length) renderEmptyRecords("没有符合筛选条件的记录"); else { $("#record-rows").replaceChildren(...state.records.map(renderRecordRow)); $("#record-cards").replaceChildren(...state.records.map(renderRecordCard)); }
  $("#record-total").textContent = `${state.total} 条记录`; const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0; const end = Math.min(state.page * state.pageSize, state.total); $("#page-range").textContent = `${start}–${end} / ${state.total}`; $("#page-label").textContent = `第 ${state.page} 页`; $("#previous").disabled = state.page <= 1; $("#next").disabled = end >= state.total; selectionChanged();
}

async function mediaData(path) { if (!state.media.has(path)) state.media.set(path, bridge.apiGet("media-data", { path }).then((result) => result.data_url)); return state.media.get(path); }

function appendMedia(parent, segments) {
  const images = segments.filter((segment) => segment.type === "image" && segment.path); if (!images.length) return;
  const grid = element("div", "media-grid");
  images.forEach((segment) => { const tile = element("button", "media-tile"); tile.type = "button"; const skeleton = element("div", "skeleton"); skeleton.style.width = "65%"; tile.append(skeleton); grid.append(tile); mediaData(segment.path).then((source) => { const image = document.createElement("img"); image.src = source; image.alt = "群典图片"; tile.replaceChildren(image); tile.addEventListener("click", () => openLightbox(images, images.indexOf(segment))); }).catch((error) => { const broken = element("div", "media-placeholder"); broken.append(icon("alert"), element("strong", "", "图片不可用"), element("small", "", error.message || segment.path)); tile.replaceChildren(broken); tile.disabled = true; }); }); parent.append(grid);
}

function appendSegmentContent(parent, segments) { const text = segments.filter((segment) => segment.type === "text").map((segment) => segment.text || "").join(""); if (text) parent.append(element("div", "detail-text", text)); appendMedia(parent, segments); }

function openDrawer(record) {
  state.activeRecord = record; const author = authorOf(record); $("#drawer-title").textContent = author.nickname || (record.type === "forward" ? "合并转发" : "未知发送者"); const content = $("#drawer-content"); content.replaceChildren();
  const meta = element("div", "detail-meta"); [["群号", record.group_id], ["记录时间", dateParts(record.recorded_at).join(" ")], ["记录 ID", record.id], ["状态", record.broken ? "异常" : "正常"]].forEach(([label, value]) => { const item = element("div"); item.append(element("span", "", label), element("strong", "", String(value || "—"))); meta.append(item); }); content.append(meta);
  if (record.type === "message") appendSegmentContent(content, record.segments || []); else (record.nodes || []).forEach((node, index) => { const nodeBox = element("section", "forward-node"); const heading = element("h3"); heading.append(document.createTextNode(node.author?.nickname || "未知发送者"), element("small", "", node.author?.user_id || `节点 ${index + 1}`)); nodeBox.append(heading); appendSegmentContent(nodeBox, node.segments || []); content.append(nodeBox); });
  $("#drawer-preview").hidden = record.type !== "message"; $("#drawer-backdrop").hidden = false; $("#record-drawer").classList.add("open"); $("#record-drawer").setAttribute("aria-hidden", "false");
}

function closeDrawer() { $("#record-drawer").classList.remove("open"); $("#record-drawer").setAttribute("aria-hidden", "true"); window.setTimeout(() => { $("#drawer-backdrop").hidden = true; }, 270); }

async function openLightbox(segments, index) {
  state.lightboxItems = segments; state.lightboxIndex = index; $("#lightbox").hidden = false; await showLightboxItem();
}
async function showLightboxItem() { const item = state.lightboxItems[state.lightboxIndex]; if (!item) return; try { $("#lightbox-image").src = await mediaData(item.path); $("#lightbox-caption").textContent = `${state.lightboxIndex + 1} / ${state.lightboxItems.length}`; } catch (error) { notify(error.message, true); } $("#lightbox-prev").disabled = state.lightboxIndex <= 0; $("#lightbox-next").disabled = state.lightboxIndex >= state.lightboxItems.length - 1; }

async function previewCard(record) {
  try { const data = await task(() => bridge.apiPost("preview", { group_id: record.group_id, record_id: record.id })); const area = $("#preview-area"); area.hidden = false; const title = element("div", "section-title-row"); title.append(element("div", "", "")); title.firstChild.append(element("h2", "", "卡片预览"), element("p", "", "使用当前已保存配置生成。")); const grid = element("div", "preview-grid"); (data.images || []).forEach((source) => { const image = document.createElement("img"); image.src = source; image.alt = "群典卡片预览"; grid.append(image); }); area.replaceChildren(title, grid); activateTab("config"); area.scrollIntoView({ behavior: "smooth", block: "start" }); closeDrawer(); } catch (error) { notify(error.message, true); }
}

async function loadConfig() {
  const value = await bridge.apiGet("config"); state.config = structuredClone(value); state.draft = structuredClone(value); setDirty(false); renderConfig(); $("#migration-path").value = value.storage_subdir || ""; $("#current-path").value = state.storageRoot || value.storage_subdir || "";
}

function applyAdvancedJson() {
  try { const value = JSON.parse($("#config-editor").value); if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("根节点必须是对象"); state.draft = value; $("#json-error").hidden = true; setDirty(); renderConfig(); notify("JSON 已应用到结构化表单。"); return true; } catch (error) { $("#json-error").textContent = `JSON 无效：${error.message}`; $("#json-error").hidden = false; $("#advanced-config").open = true; notify("配置 JSON 无效，无法保存。", true); return false; }
}

async function saveConfig() {
  if (!applyAdvancedJson()) return;
  try { const saved = await task(() => bridge.apiPost("config/save", state.draft)); state.config = structuredClone(saved); state.draft = structuredClone(saved); setDirty(false); renderConfig(); notify("配置已校验、保存并热更新。"); } catch (error) { notify(error.message, true); }
}

async function loadAudit() { const data = await bridge.apiGet("audit", { limit: 1000 }); state.audit = (data.items || []).slice().sort((a, b) => String(b.deleted_at).localeCompare(String(a.deleted_at))); renderAudit(); }
function renderAudit() {
  const group = $("#audit-group").value.trim().toLowerCase(); const operator = $("#audit-operator").value.trim().toLowerCase(); const source = $("#audit-source").value;
  const items = state.audit.filter((item) => (!group || String(item.group_id).toLowerCase().includes(group)) && (!operator || String(item.deleted_by).toLowerCase().includes(operator)) && (!source || item.source === source));
  const rows = items.map((item) => { const tr = document.createElement("tr"); [item.deleted_at, item.group_id, item.record_id, item.deleted_by, item.source].forEach((value) => tr.append(element("td", "", String(value || "—")))); return tr; });
  if (!rows.length) { const td = element("td", "empty-cell", "没有符合条件的审计记录"); td.colSpan = 5; const tr = document.createElement("tr"); tr.append(td); rows.push(tr); } $("#audit-rows").replaceChildren(...rows);
  $("#audit-cards").replaceChildren(...items.map((item) => { const card = element("article", "record-card audit-card"); card.append(element("strong", "", item.deleted_at || "—"), element("div", "summary-text", `群 ${item.group_id} · ${item.record_id}`), element("div", "record-card-meta", `${item.deleted_by} · ${item.source}`)); return card; }));
}

function activateTab(name) {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name)); $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${name}-panel`));
  if (name === "audit" && !state.audit.length) task(loadAudit).catch((error) => notify(error.message, true));
}

function confirmAction(title, message) { const dialog = $("#confirm-dialog"); $("#dialog-title").textContent = title; $("#dialog-message").textContent = message; dialog.showModal(); return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true })); }

async function deleteRecords(ids) {
  if (!ids.length) return; if (!await confirmAction("删除群典", `确定永久删除所选 ${ids.length} 条记录吗？此操作会写入删除审计。`)) return;
  try { const result = await task(() => bridge.apiPost("records/delete", { group_id: $("#group").value, record_ids: ids })); ids.forEach((id) => state.selected.delete(id)); closeDrawer(); notify(`已删除 ${result.deleted} 条记录。`); await task(loadStats); } catch (error) { notify(error.message, true); }
}

function showImportInspection(inspection) { const fields = [["新增记录", inspection.added], ["重复跳过", inspection.duplicates], ["ID 冲突", inspection.conflicts], ["新增图片", formatBytes(inspection.image_bytes)]]; const box = $("#import-inspection"); box.hidden = false; box.replaceChildren(...fields.map(([label, value]) => { const item = element("div", "inspection-item"); item.append(element("span", "", label), element("strong", "", String(value ?? 0))); return item; })); }

async function inspectAndImport() {
  const file = $("#import-file").files[0]; if (!file) return notify("请先选择 ZIP 备份。", true);
  try { const inspection = await task(() => bridge.upload("backup/import", file)); showImportInspection(inspection); if (inspection.missing_images) return notify(`预检失败：缺少 ${inspection.missing_images} 张被记录引用的图片。`, true);
    const summary = `将新增 ${inspection.added} 条，跳过重复 ${inspection.duplicates} 条，跳过 ID 冲突 ${inspection.conflicts} 条，新增图片 ${formatBytes(inspection.image_bytes)}。`;
    if (!await confirmAction("确认导入备份", `${summary} 确认执行合并吗？`)) return;
    const result = await task(() => bridge.apiPost("backup/import/commit", { token: inspection.token, restore_settings: $("#restore-settings").checked })); notify(`导入完成：新增 ${result.added} 条，跳过重复 ${result.duplicates} 条。`); await task(() => Promise.all([loadStats(), loadConfig()]));
  } catch (error) { notify(error.message, true); }
}

async function migrateStorage() { const target = $("#migration-path").value.trim(); if (!target) return notify("请输入目标相对目录。", true); if (!await confirmAction("迁移存储目录", `确定将全部数据迁移到 ${target} 吗？迁移期间请勿操作群典。`)) return; try { const result = await task(() => bridge.apiPost("storage/migrate", { storage_subdir: target })); notify(`迁移完成，旧目录备份位于：${result.backup_root}`); await task(loadConfig); } catch (error) { notify(error.message, true); } }

hydrateIcons(); $("#page-size").value = String(state.pageSize);
$$('.tab').forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
$("#refresh").addEventListener("click", () => task(() => Promise.all([loadStats(), loadConfig()])).then(() => notify("数据已刷新。")).catch((error) => notify(error.message, true)));
$("#search-button").addEventListener("click", () => { state.page = 1; task(loadRecords).catch((error) => notify(error.message, true)); });
$("#search").addEventListener("keydown", (event) => { if (event.key === "Enter") $("#search-button").click(); });
$("#group").addEventListener("change", () => { state.page = 1; state.selected.clear(); task(loadRecords).catch((error) => notify(error.message, true)); });
$("#previous").addEventListener("click", () => { state.page -= 1; task(loadRecords).catch((error) => notify(error.message, true)); }); $("#next").addEventListener("click", () => { state.page += 1; task(loadRecords).catch((error) => notify(error.message, true)); });
$("#page-size").addEventListener("change", () => { state.pageSize = Number($("#page-size").value); sessionStorage.setItem("iconic-quotes-page-size", String(state.pageSize)); state.page = 1; task(loadRecords).catch((error) => notify(error.message, true)); });
$("#select-page").addEventListener("change", () => { let skipped = false; state.records.forEach((record) => { if (!$("#select-page").checked) state.selected.delete(record.id); else if (state.selected.size < 100) state.selected.add(record.id); else skipped = true; }); if (skipped) notify("已达到每次 100 条的批量删除上限。", true); selectionChanged(); });
$("#delete-selected").addEventListener("click", () => deleteRecords([...state.selected])); $("#filter-toggle").addEventListener("click", () => $("#record-filters").classList.toggle("open"));
$("#close-drawer").addEventListener("click", closeDrawer); $("#drawer-backdrop").addEventListener("click", closeDrawer); $("#drawer-delete").addEventListener("click", () => state.activeRecord && deleteRecords([state.activeRecord.id])); $("#drawer-preview").addEventListener("click", () => state.activeRecord && previewCard(state.activeRecord));
$("#lightbox-close").addEventListener("click", () => { $("#lightbox").hidden = true; }); $("#lightbox-prev").addEventListener("click", () => { state.lightboxIndex -= 1; showLightboxItem(); }); $("#lightbox-next").addEventListener("click", () => { state.lightboxIndex += 1; showLightboxItem(); });
$("#open-group-override").addEventListener("click", openOverrideGroup); $("#override-group-select").addEventListener("change", () => { if ($("#override-group-select").value) { $("#override-group-input").value = ""; openOverrideGroup(); } }); $("#clear-group-override").addEventListener("click", clearOverride);
$("#apply-json").addEventListener("click", applyAdvancedJson); $("#format-json").addEventListener("click", () => { try { $("#config-editor").value = JSON.stringify(JSON.parse($("#config-editor").value), null, 2); $("#json-error").hidden = true; } catch (error) { $("#json-error").textContent = `JSON 无效：${error.message}`; $("#json-error").hidden = false; } });
$("#save-config").addEventListener("click", saveConfig); $("#discard-config").addEventListener("click", () => { state.draft = structuredClone(state.config); setDirty(false); renderConfig(); notify("已放弃未保存的配置修改。"); });
$("#export").addEventListener("click", () => task(() => bridge.download("backup/export", {}, "iconic-quotes-backup.zip")).catch((error) => notify(error.message, true))); $("#import").addEventListener("click", inspectAndImport); $("#migrate").addEventListener("click", migrateStorage);
function updateImportFileLabel() { const file = $("#import-file").files[0]; $("#file-name").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "最大 1 GB，仅支持 ZIP"; }
$("#import-file").addEventListener("change", updateImportFileLabel);
["dragenter", "dragover"].forEach((name) => $("#drop-zone").addEventListener(name, (event) => { event.preventDefault(); $("#drop-zone").classList.add("dragging"); })); ["dragleave", "drop"].forEach((name) => $("#drop-zone").addEventListener(name, () => $("#drop-zone").classList.remove("dragging")));
$("#drop-zone").addEventListener("drop", (event) => { event.preventDefault(); const files = event.dataTransfer?.files; if (!files?.length) return; if (!files[0].name.toLowerCase().endsWith(".zip")) return notify("只支持 ZIP 备份文件。", true); $("#import-file").files = files; updateImportFileLabel(); });
$("#refresh-audit").addEventListener("click", () => task(loadAudit).then(() => notify("审计记录已刷新。")).catch((error) => notify(error.message, true))); ["#audit-group", "#audit-operator"].forEach((selector) => $(selector).addEventListener("input", renderAudit)); $("#audit-source").addEventListener("change", renderAudit);
document.addEventListener("click", (event) => { if (!event.target.closest(".row-menu")) $$(".row-menu-pop").forEach((menu) => { menu.hidden = true; }); });

try { await task(() => Promise.all([loadStats(), loadConfig()])); } catch (error) { $("#header-status").textContent = "插件数据加载失败"; notify(error.message, true); }
