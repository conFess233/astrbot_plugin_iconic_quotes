// 配置模块：全局配置、群聊覆盖、高级 JSON 三种编辑方式共用一份草稿；
// 右侧卡片列出全部改动，右下悬浮按钮变形为唯一的保存入口。

import { $, $$, bridge, confirmAction, el, hydrate, icon, reducedMotion, segBar, toast } from "./core.js";

const ROLE_OPTIONS = [["bot_admin", "Bot 管理员"], ["owner", "群主"], ["admin", "群管理员"], ["member", "群成员"], ["everyone", "所有人"]];

const SECTIONS = [
  { id: "trigger", title: "触发与发送", description: "关键词、发送条数与输出形式。", fields: [
    { key: "add_keyword_enabled", label: "启用添加关键词", type: "boolean", hint: "关闭后仍可使用 /添加群典 指令。" },
    { key: "add_keywords", label: "添加关键词", type: "list", hint: "每行一个关键词；需要同时引用消息。" },
    { key: "query_keyword_enabled", label: "启用查询关键词", type: "boolean", hint: "关闭后仍可使用 /群典 指令。" },
    { key: "query_keywords", label: "查询关键词", type: "list", hint: "每行一个关键词。" },
    { key: "burst_keyword_enabled", label: "启用爆典关键词", type: "boolean", hint: "关闭后仍可使用 /爆典 指令。" },
    { key: "burst_keywords", label: "爆典关键词", type: "list", hint: "每行一个；用法：爆典 @某人 [页码]。" },
    { key: "burst_page_size", label: "爆典每页条数", type: "number", min: 1, max: 100 },
    { key: "burst_time_mode", label: "爆典时间显示", type: "select", options: [["text", "独立节点"], ["native", "原生时间"], ["none", "不显示"]], hint: "原生时间为实验功能，是否显示取决于 OneBot 实现。" },
    { key: "send_count", label: "最大发送条数", type: "number", min: 1, max: 10 },
    { key: "random_send_count", label: "随机发送条数", type: "boolean", hint: "每次在 1 到最大发送条数之间随机。" },
    { key: "send_mode", label: "单条发送方式", type: "select", options: [["text", "文字"], ["card", "图片卡片"]], hint: "含表情、贴纸或回复的记录始终按原消息发送。" },
    { key: "aggregate_multiple", label: "多条聚合为合并转发", type: "boolean" },
    { key: "allow_bot_authors", label: "允许收录 Bot 消息", type: "boolean" },
  ] },
  { id: "poke", title: "戳一戳", description: "群成员戳机器人时随机发送群典。", fields: [
    { key: "poke_enabled", label: "启用戳一戳群典", type: "boolean", hint: "沿用查询权限、名单与全局冷却。" },
    { key: "poke_send_count", label: "戳一戳发送条数", type: "number", min: 1, max: 10 },
    { key: "poke_send_mode", label: "戳一戳发送方式", type: "select", options: [["text", "正常"], ["card", "图片"]] },
  ] },
  { id: "help", title: "帮助消息", description: "主动帮助与参数错误后附带的用法。", fields: [
    { key: "help_enabled", label: "启用帮助功能", type: "boolean", hint: "关闭后不发送主动帮助，也不附带用法。" },
    { key: "help_keywords", label: "帮助子关键词", type: "list", hint: "用于 /群典 <关键词> 与 群典 <关键词>。" },
    { key: "help_overview_template", label: "总帮助模板", type: "textarea", hint: "留空时自动组合下方非空模板。" },
    { key: "help_add_template", label: "添加用法", type: "textarea" },
    { key: "help_query_template", label: "查询用法", type: "textarea" },
    { key: "help_burst_template", label: "爆典用法", type: "textarea" },
    { key: "help_info_template", label: "统计用法", type: "textarea" },
    { key: "help_delete_template", label: "删除用法", type: "textarea" },
    { key: "help_help_template", label: "帮助用法", type: "textarea", hint: "模板可用 {add_keywords}、{query_keywords}、{burst_keywords}、{burst_page_size}、{max_query_keyword_chars}；留空可单独关闭该项提示。" },
  ] },
  { id: "limits", title: "容量与限制", description: "本地存储与单条消息的安全边界。", fields: [
    { key: "max_records_per_group", label: "单群记录上限", type: "number", min: 1, max: 100000 },
    { key: "max_media_mb", label: "媒体容量上限（MB）", type: "number", min: 1, max: 102400, hint: "群典图片与头像缓存合计；不足时只淘汰头像。" },
    { key: "max_image_mb", label: "单图上限（MB）", type: "number", min: 1, max: 100 },
    { key: "max_images_per_record", label: "单条图片上限", type: "number", min: 1, max: 100 },
    { key: "max_forward_nodes", label: "转发节点上限", type: "number", min: 1, max: 200 },
    { key: "max_text_chars", label: "单条文字上限", type: "number", min: 1, max: 100000 },
    { key: "max_forward_text_chars", label: "转发累计文字上限", type: "number", min: 1, max: 1000000 },
    { key: "max_query_keyword_chars", label: "查询关键词长度上限", type: "number", min: 1, max: 1000 },
    { key: "max_reply_depth", label: "回复快照深度", type: "number", min: 1, max: 10 },
    { key: "max_nested_forward_depth", label: "嵌套转发深度", type: "number", min: 1, max: 10 },
    { key: "delete_preview_limit", label: "删除预览上限", type: "number", min: 1, max: 50 },
    { key: "audit_limit", label: "删除审计保留条数", type: "number", min: 1, max: 100000 },
  ] },
  { id: "permissions", title: "权限", description: "分别控制添加、查询、爆典、统计、删除与帮助。", fields: [
    { key: "add_roles", label: "添加", type: "roles" },
    { key: "query_roles", label: "随机查询", type: "roles" },
    { key: "burst_roles", label: "爆典", type: "roles" },
    { key: "info_roles", label: "查看统计", type: "roles" },
    { key: "delete_roles", label: "删除", type: "roles" },
    { key: "help_roles", label: "主动帮助", type: "roles" },
  ] },
  { id: "lists", title: "名单", description: "黑名单优先；白名单为空时不限制。ID 每行一个。", fields: [
    { key: "group_blacklist", label: "群黑名单", type: "list" },
    { key: "group_whitelist", label: "群白名单", type: "list" },
    { key: "user_blacklist", label: "用户黑名单", type: "list" },
    { key: "user_whitelist", label: "用户白名单", type: "list" },
    { key: "excluded_author_ids", label: "禁止被收录的 QQ", type: "list", hint: "同样作用于回复快照与转发节点。" },
  ] },
  { id: "aliases", title: "作者别名", description: "只纠正合并转发节点与回复快照的作者，不改写存档；保存前预览影响范围。", fields: [
    { key: "author_aliases", label: "别名表", type: "aliases", hint: "同一群内一个别名只能绑定一个 QQ。" },
  ] },
  { id: "card", title: "卡片与头像", description: "图片卡片尺寸、自定义 CSS 与头像缓存。", fields: [
    { key: "card_width", label: "卡片宽度（px）", type: "number", min: 480, max: 3000 },
    { key: "card_auto_height", label: "按内容裁切高度", type: "boolean", hint: "收缩画布并裁去右侧、底部留白。" },
    { key: "card_min_height", label: "最小高度（px）", type: "number", min: 240, max: 3000 },
    { key: "card_max_height", label: "最大高度（px）", type: "number", min: 480, max: 6000 },
    { key: "card_custom_css", label: "自定义 CSS", type: "textarea", hint: "不允许 @import、url() 等外部资源。" },
    { key: "localize_avatars", label: "本地缓存头像", type: "boolean", hint: "只用于卡片与后台；合并转发头像仍由 QQ 解析。" },
    { key: "avatar_cache_ttl_days", label: "头像缓存有效天数", type: "number", min: 0, max: 3650, hint: "0 表示不自动刷新。" },
  ] },
  { id: "cooldown", title: "冷却与重试", description: "全局冷却与发送失败重试。", fields: [
    { key: "global_cooldown_ms", label: "全局冷却（毫秒）", type: "number", min: 0, max: 60000 },
    { key: "cooldown_message", label: "冷却提示", type: "text" },
    { key: "send_retry_count", label: "发送重试次数", type: "number", min: 0, max: 5 },
    { key: "send_retry_delay_ms", label: "重试间隔（毫秒）", type: "number", min: 100, max: 10000 },
    { key: "retry_on_ambiguous_failure", label: "结果未知时也重试", type: "boolean", hint: "超时或断线后重试可能造成重复消息，默认关闭。" },
    { key: "nested_forward_fallback_message", label: "转发降级提示", type: "text" },
    { key: "nested_forward_unknown_message", label: "转发结果未知提示", type: "text" },
  ] },
];

const FIELDS = SECTIONS.flatMap((section) => section.fields.map((field) => ({ ...field, section: section.id })));
const FIELD_BY_KEY = new Map(FIELDS.map((field) => [field.key, field]));
const OVERRIDE_KEYS = [
  "poke_enabled", "poke_send_count", "poke_send_mode",
  "add_keyword_enabled", "add_keywords", "query_keyword_enabled", "query_keywords", "max_query_keyword_chars",
  "burst_keyword_enabled", "burst_keywords", "burst_page_size", "burst_time_mode",
  "help_enabled", "help_keywords", "help_roles",
  "help_overview_template", "help_add_template", "help_query_template", "help_burst_template",
  "help_info_template", "help_delete_template", "help_help_template",
  "send_count", "random_send_count", "send_mode", "aggregate_multiple", "allow_bot_authors",
  "max_records_per_group", "add_roles", "query_roles", "burst_roles", "info_roles", "delete_roles",
  "user_blacklist", "user_whitelist", "excluded_author_ids",
  "nested_forward_fallback_message", "nested_forward_unknown_message",
];

const state = {
  saved: null,
  draft: null,
  scope: "global",
  section: SECTIONS[0].id,
  overrideGroup: "",
  saving: false,
  groups: () => [],
  onChange: () => {},
};

const clone = (value) => structuredClone(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function display(field, value) {
  if (value === undefined) return "继承全局";
  if (field?.type === "boolean") return value ? "开启" : "关闭";
  if (field?.type === "select") return (field.options.find(([key]) => key === value) || [value, value])[1];
  if (field?.type === "roles") return value.map((role) => (ROLE_OPTIONS.find(([key]) => key === role) || [role, role])[1]).join("、") || "无";
  if (Array.isArray(value)) return value.join("、") || "（空）";
  if (typeof value === "string") return value.length > 40 ? `${value.slice(0, 40)}…` : value || "（空）";
  return String(value);
}

// ── 差异 ─────────────────────────────────────

export function changes() {
  if (!state.saved || !state.draft) return [];
  const result = [];
  for (const field of FIELDS) {
    if (field.key === "author_aliases") continue;
    if (!same(state.saved[field.key], state.draft[field.key])) {
      result.push({ key: field.key, name: field.label, from: display(field, state.saved[field.key]), to: display(field, state.draft[field.key]) });
    }
  }
  const before = state.saved.group_overrides || {};
  const after = state.draft.group_overrides || {};
  for (const group of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
    const a = before[group] || {};
    const b = after[group] || {};
    for (const key of [...new Set([...Object.keys(a), ...Object.keys(b)])]) {
      if (same(a[key], b[key])) continue;
      const field = FIELD_BY_KEY.get(key);
      result.push({ key: `override:${group}:${key}`, group, name: `群 ${group} · ${field?.label || key}`, from: display(field, a[key]), to: display(field, b[key]) });
    }
  }
  if (!same(state.saved.author_aliases || {}, state.draft.author_aliases || {})) {
    const count = (map) => Object.values(map || {}).reduce((sum, targets) => sum + Object.values(targets).reduce((n, list) => n + list.length, 0), 0);
    result.push({ key: "author_aliases", name: "作者别名", from: `${count(state.saved.author_aliases)} 个别名`, to: `${count(state.draft.author_aliases)} 个别名` });
  }
  return result;
}

export const isDirty = () => changes().length > 0;

function sectionDirty(sectionId) {
  return changes().some((change) => FIELD_BY_KEY.get(change.key)?.section === sectionId);
}

// ── 控件 ─────────────────────────────────────

function parseLines(value) {
  return [...new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
}

function control(field, value, onChange, { disabled = false, id } = {}) {
  const common = { id, disabled };
  switch (field.type) {
    case "boolean": {
      const button = el("button.switch", { ...common, type: "button", role: "switch", "aria-checked": String(Boolean(value)), class: `switch${value ? " on" : ""}`, "aria-label": field.label });
      button.addEventListener("click", () => onChange(!value));
      return button;
    }
    case "select": {
      const group = el("div.segmented", { role: "radiogroup", "aria-label": field.label });
      field.options.forEach(([key, label]) => {
        const option = el("button", { type: "button", role: "radio", "aria-checked": String(value === key), class: value === key ? "on" : "", disabled, text: label });
        option.addEventListener("click", () => onChange(key));
        group.append(option);
      });
      return group;
    }
    case "number": {
      const input = el("input.input.num", { ...common, type: "number", min: field.min, max: field.max, step: "1", value: String(value ?? "") });
      input.addEventListener("change", () => {
        const number = Number(input.value);
        if (!Number.isInteger(number) || number < field.min || number > field.max) {
          input.classList.remove("err");
          void input.offsetWidth;
          input.classList.add("err");
          input.setAttribute("aria-invalid", "true");
          input.title = `请输入 ${field.min} 到 ${field.max} 之间的整数`;
          return;
        }
        input.removeAttribute("aria-invalid");
        input.classList.remove("err");
        onChange(number);
      });
      if (field.max - field.min > 200) return input;
      return el("div.seg-input", {}, el("div.row", {}, segBar(24, ((value ?? field.min) - field.min) / (field.max - field.min || 1)), input));
    }
    case "list":
    case "textarea": {
      const area = el("textarea.input.area", { ...common, rows: field.type === "list" ? 3 : 4, spellcheck: "false" });
      area.value = field.type === "list" ? (value || []).join("\n") : value || "";
      area.addEventListener("change", () => onChange(field.type === "list" ? parseLines(area.value) : area.value));
      return area;
    }
    case "roles": {
      const group = el("div.role-chips", { role: "group", "aria-label": field.label });
      ROLE_OPTIONS.forEach(([key, label]) => {
        const on = (value || []).includes(key);
        const chip = el("button.role-chip", { type: "button", "aria-pressed": String(on), class: `role-chip${on ? " on" : ""}`, disabled, text: label });
        chip.addEventListener("click", () => {
          const next = on ? value.filter((role) => role !== key) : [...value, key];
          if (!next.length) return toast("至少保留一个角色", "info");
          onChange(next);
        });
        group.append(chip);
      });
      return group;
    }
    case "aliases":
      return aliasEditor(value || {}, onChange);
    default: {
      const input = el("input.input", { ...common, type: "text", value: value ?? "" });
      input.addEventListener("change", () => onChange(input.value));
      return input;
    }
  }
}

function aliasEditor(value, onChange) {
  const rows = Object.entries(value).flatMap(([group, targets]) =>
    Object.entries(targets).map(([user, aliases]) => ({ group, user, aliases: aliases.join("，") })));
  const box = el("div.alias-editor");
  const commit = () => {
    const result = {};
    for (const row of rows) {
      const group = row.group.trim();
      const user = row.user.trim();
      const aliases = [...new Set(row.aliases.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean))];
      if (!group || !user || !aliases.length) continue;
      result[group] ||= {};
      result[group][user] = [...new Set([...(result[group][user] || []), ...aliases])];
    }
    onChange(result);
  };
  const render = () => {
    box.replaceChildren(
      ...rows.map((row, index) => {
        const input = (key, label, placeholder, numeric) => {
          const node = el("input.input", { type: "text", value: row[key], placeholder, "aria-label": label, inputmode: numeric ? "numeric" : null });
          node.addEventListener("change", () => { row[key] = node.value; commit(); });
          return node;
        };
        return el("div.alias-row", {},
          input("group", "群号", "群号", true),
          input("user", "QQ", "QQ", true),
          input("aliases", "别名", "别名，用逗号分隔", false),
          el("button.icon-btn", { type: "button", "aria-label": "删除这一行", onclick: () => { rows.splice(index, 1); render(); commit(); } }, icon("x")));
      }),
      el("button.btn.btn-ghost.btn-sm", { type: "button", onclick: () => { rows.push({ group: state.overrideGroup || "", user: "", aliases: "" }); render(); } }, "添加别名"),
    );
  };
  render();
  return box;
}

function formRow(field, changed, controlNode, extra) {
  const id = `cfg-${field.key}`;
  return el("div.form-row", { class: `form-row${changed ? " changed" : ""}`, dataset: { key: field.key } },
    el("div.lbl", {}, el("label", { for: id }, el("strong", { text: field.label })), extra),
    el("div", {}, controlNode, field.hint ? el("div.help", { text: field.hint }) : null));
}

function flash(key) {
  const row = $(`.form-row[data-key="${CSS.escape(key)}"]`);
  if (!row || reducedMotion()) return;
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 320);
}

function setValue(key, value) {
  state.draft[key] = value;
  update(key);
}

// ── 渲染 ─────────────────────────────────────

function renderAnchors() {
  $("#anchors").replaceChildren(...SECTIONS.map((section) => el("a", {
    href: `#sec-${section.id}`,
    class: state.section === section.id ? "on" : "",
    "aria-current": state.section === section.id ? "true" : null,
  }, section.title, sectionDirty(section.id) ? el("span.mod", { "aria-label": "有修改" }) : null)));
}

function renderGlobal() {
  const saved = state.saved;
  const draft = state.draft;
  return SECTIONS.map((section) => el("section.card.card-pad.cfg-section", { id: `sec-${section.id}` },
    el("div.card-title", {}, el("div", {}, el("h3", { text: section.title }), el("p", { text: section.description }))),
    section.fields.map((field) => formRow(
      field,
      !same(saved[field.key], draft[field.key]),
      control(field, draft[field.key], (value) => setValue(field.key, value), { id: `cfg-${field.key}` }),
    ))));
}

function renderOverride() {
  const known = [...new Set([...state.groups(), ...Object.keys(state.draft.group_overrides || {})])].sort();
  const select = el("select.input", { id: "override-select", "aria-label": "选择群聊" },
    el("option", { value: "", text: known.length ? "选择群聊" : "暂无群聊" }),
    known.map((id) => el("option", { value: id, text: `${id}${state.draft.group_overrides?.[id] ? " · 已覆盖" : ""}` })));
  select.value = state.overrideGroup;
  select.addEventListener("change", () => { state.overrideGroup = select.value; renderBody(); });
  const input = el("input.input", { type: "text", inputmode: "numeric", placeholder: "或输入群号", "aria-label": "输入群号" });
  const open = el("button.btn.btn-ghost", { type: "button", text: "打开" });
  open.addEventListener("click", () => {
    const value = input.value.trim();
    if (!/^\d+$/.test(value) || value === "0") {
      input.classList.remove("err");
      void input.offsetWidth;
      input.classList.add("err");
      return toast("群号必须是正整数", "bad");
    }
    state.overrideGroup = value;
    renderBody();
  });
  const picker = el("div.card.card-pad", {},
    el("div.card-title", {}, el("div", {}, el("h3", { text: "群聊覆盖" }), el("p", { text: "只覆盖打开开关的字段，其余继续继承全局配置。" }))),
    el("div.picker", {}, el("label.field", {}, "群聊", select), el("label.field", {}, "群号", input), open));
  if (!state.overrideGroup) return [picker];
  const group = state.overrideGroup;
  const overrides = state.draft.group_overrides || (state.draft.group_overrides = {});
  const current = overrides[group] || {};
  const savedCurrent = state.saved.group_overrides?.[group] || {};
  const rows = OVERRIDE_KEYS.map((key) => {
    const field = FIELD_BY_KEY.get(key);
    const enabled = Object.hasOwn(current, key);
    const toggle = el("button.switch.switch-sm", {
      type: "button", role: "switch", "aria-checked": String(enabled), class: `switch switch-sm${enabled ? " on" : ""}`,
      "aria-label": `覆盖${field.label}`, title: enabled ? "取消覆盖" : "覆盖此项",
    });
    toggle.addEventListener("click", () => {
      const next = { ...current };
      if (enabled) delete next[key]; else next[key] = clone(state.draft[key]);
      if (Object.keys(next).length) overrides[group] = next; else delete overrides[group];
      update(key);
    });
    const node = control(field, enabled ? current[key] : state.draft[key], (value) => {
      overrides[group] = { ...current, [key]: value };
      update(key);
    }, { disabled: !enabled, id: `cfg-${key}` });
    return formRow(field, !same(current[key], savedCurrent[key]), node, el("span.override-toggle", {}, toggle, enabled ? "覆盖中" : "继承全局"));
  });
  const clear = el("button.btn.btn-danger.btn-sm", { type: "button", text: "清空本群覆盖", disabled: !Object.keys(current).length });
  clear.addEventListener("click", async () => {
    if (!await confirmAction({ title: "清空群聊覆盖", message: `将从草稿中移除群 ${group} 的 ${Object.keys(current).length} 个覆盖字段，保存后生效。`, confirm: "清空覆盖", danger: true })) return;
    delete overrides[group];
    update();
  });
  return [picker, el("section.card.card-pad", {},
    el("div.card-title", {}, el("div", {}, el("h3", { text: `群 ${group}` }), el("p", { text: `${Object.keys(current).length} 个字段覆盖中` })), clear),
    rows)];
}

function renderJson() {
  const area = el("textarea.input.area.code", { id: "config-json", rows: 24, spellcheck: "false", "aria-label": "完整配置 JSON" });
  area.value = JSON.stringify(state.draft, null, 2);
  const error = el("div.inline-error", { id: "json-error", role: "alert", hidden: true });
  const apply = el("button.btn.btn-primary", { type: "button", text: "应用到草稿" });
  apply.addEventListener("click", () => {
    let parsed;
    try {
      parsed = JSON.parse(area.value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("顶层必须是对象");
    } catch (exc) {
      error.hidden = false;
      error.textContent = `JSON 无法解析：${exc.message}`;
      area.classList.add("err");
      return;
    }
    error.hidden = true;
    area.classList.remove("err");
    const next = clone(state.draft);
    for (const key of Object.keys(next)) if (Object.hasOwn(parsed, key)) next[key] = parsed[key];
    state.draft = next;
    update();
    toast("已应用到草稿，保存前仍可继续修改");
  });
  const format = el("button.btn.btn-ghost", { type: "button", text: "重新生成" });
  format.addEventListener("click", () => { area.value = JSON.stringify(state.draft, null, 2); error.hidden = true; });
  return [el("section.card.card-pad", {},
    el("div.card-title", {}, el("div", {}, el("h3", { text: "高级 JSON" }), el("p", { text: "批量粘贴完整配置；应用只更新草稿，最终由后端校验。" }))),
    error, area, el("div.inline-actions", {}, apply, format))];
}

function renderBody() {
  $$("#scope button").forEach((button) => {
    const on = button.dataset.scope === state.scope;
    button.classList.toggle("on", on);
    button.setAttribute("aria-checked", String(on));
  });
  $("#anchors").hidden = state.scope !== "global";
  if (state.scope === "global") renderAnchors();
  const body = $("#config-body");
  body.replaceChildren(...(state.scope === "global" ? renderGlobal() : state.scope === "override" ? renderOverride() : renderJson()));
  hydrate(body);
}

function renderDraft() {
  const list = changes();
  const card = $("#config-side");
  card.replaceChildren(list.length
    ? el("div.side-body", {}, el("h3", { text: `${list.length} 项改动待保存` }),
      el("ul.diff", {}, list.map((item) => el("li", {},
        el("span.name", { text: item.name }),
        el("span.from", { text: item.from }),
        el("span.to", { text: item.to })))))
    : el("div.side-body", {}, el("h3", { text: "没有未保存的修改" }),
      el("p.muted", { text: "修改左侧任意配置后，改动会列在这里，右下角按钮变为保存入口。保存成功后立即生效。" })));
  state.onChange(list.length);
}

/** 修改后只局部刷新：保持当前滚动位置，并闪烁所在行。 */
function update(key) {
  const scrollY = window.scrollY;
  const focusedKey = document.activeElement?.closest?.(".form-row")?.dataset.key;
  renderBody();
  renderDraft();
  window.scrollTo(0, scrollY);
  if (key) flash(key);
  if (focusedKey) $(`.form-row[data-key="${CSS.escape(focusedKey)}"] button, .form-row[data-key="${CSS.escape(focusedKey)}"] input`)?.focus({ preventScroll: true });
}

// ── 加载、放弃与保存 ─────────────────────────

export async function loadConfig() {
  const value = await bridge.apiGet("config");
  delete value.status;
  state.saved = clone(value);
  state.draft = clone(value);
  if (state.saved && $("#config-body")) {
    renderBody();
    renderDraft();
  }
}

export function discard() {
  state.draft = clone(state.saved);
  $("#save-error").hidden = true;
  update();
  toast("已放弃修改");
}

export async function save(button) {
  if (state.saving || !isDirty()) return;
  const error = $("#save-error");
  error.hidden = true;
  if (!same(state.saved.author_aliases || {}, state.draft.author_aliases || {})) {
    try {
      const preview = await bridge.apiPost("aliases/preview", { author_aliases: state.draft.author_aliases || {} });
      const ok = await confirmAction({
        title: "确认作者别名",
        message: `新的别名会改变 ${preview.affected_records} 条记录中 ${preview.affected_nodes} 处作者的显示与查询归属。存档内容不会被改写。`,
        confirm: "继续保存",
        danger: false,
      });
      if (!ok) return;
    } catch (exc) {
      error.hidden = false;
      error.textContent = `别名无效：${exc.message}`;
      return;
    }
  }
  state.saving = true;
  const pill = $("#pill");
  const children = [...button.childNodes];
  button.disabled = true;
  button.replaceChildren(el("span.spin", { "aria-hidden": "true" }), "保存中");
  try {
    const saved = await bridge.apiPost("config/save", state.draft);
    delete saved.status;
    pill.classList.add("done");
    button.replaceChildren(icon("check"), "已保存");
    await new Promise((resolve) => setTimeout(resolve, reducedMotion() ? 0 : 600));
    state.saved = clone(saved);
    state.draft = clone(saved);
    update();
    toast("配置已校验、保存并立即生效");
  } catch (exc) {
    error.hidden = false;
    error.textContent = `保存失败：${exc.message}。草稿已保留，修正后可再次保存。`;
  } finally {
    state.saving = false;
    pill.classList.remove("done");
    button.replaceChildren(...children);
    button.disabled = false;
  }
}

export function initConfig({ groups, onChange }) {
  state.groups = groups;
  state.onChange = onChange;
  $("#scope").addEventListener("click", (event) => {
    const button = event.target.closest("[data-scope]");
    if (!button) return;
    state.scope = button.dataset.scope;
    renderBody();
  });
  $("#anchors").addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link) return;
    event.preventDefault();
    state.section = link.getAttribute("href").slice(5);
    renderAnchors();
    $(link.getAttribute("href"))?.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  });
  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  if (state.saved) {
    renderBody();
    renderDraft();
  }
}
