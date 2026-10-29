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
    { key: "forward_hint_nodes", label: "合并转发中插入提示", type: "boolean", hint: "开头加入“群典存档回放”与“引用丢失提示”节点；爆典的标题与页码始终保留。" },
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
    { key: "card_t2i_endpoint", label: "文转图服务地址", type: "text", hint: "留空使用 AstrBot 的文转图设置；可填本地部署的 astrbot-t2i-service，如 http://127.0.0.1:8999。" },
    { key: "card_t2i_timeout_s", label: "文转图超时（秒）", type: "number", min: 5, max: 120, hint: "仅对单独配置的服务生效；超时后改为文字发送。" },
    { key: "card_theme", label: "卡片主题", type: "select", options: [["light", "纸面"], ["dark", "墨色"]] },
    { key: "card_scale", label: "清晰度", type: "select", options: [["normal", "标准"], ["high", "高清"], ["ultra", "超清"]], hint: "设备像素比 1.0 / 1.3 / 1.8，越高图片越大。" },
    { key: "card_width", label: "卡片宽度（px）", type: "number", min: 480, max: 3000 },
    { key: "card_auto_height", label: "高度随内容变化", type: "boolean", hint: "关闭后卡片不低于最小高度。" },
    { key: "card_min_height", label: "最小高度（px）", type: "number", min: 240, max: 3000 },
    { key: "card_max_height", label: "最大高度（px）", type: "number", min: 480, max: 6000 },
    { key: "card_custom_css", label: "自定义 CSS", type: "textarea", hint: "追加在默认样式之后；不允许 @import、url() 等外部资源。" },
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
  "send_count", "random_send_count", "send_mode", "aggregate_multiple", "forward_hint_nodes", "allow_bot_authors",
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

/** 当前编辑范围下某个分区是否有改动：全局看字段本身，群聊覆盖看当前群的覆盖字段。 */
function sectionDirty(sectionId) {
  if (state.scope === "override") {
    const prefix = `override:${state.overrideGroup}:`;
    return changes().some((change) => change.key.startsWith(prefix)
      && FIELD_BY_KEY.get(change.key.slice(prefix.length))?.section === sectionId);
  }
  return changes().some((change) => FIELD_BY_KEY.get(change.key)?.section === sectionId);
}

/** 当前编辑范围下可用的分区；群聊覆盖只显示含可覆盖字段的分区。 */
function visibleSections() {
  if (state.scope !== "override") return SECTIONS;
  return SECTIONS.filter((section) => section.fields.some((field) => OVERRIDE_KEYS.includes(field.key)));
}

// ── 控件：就地更新自身状态，不触发整页重建 ───

function parseLines(value) {
  return [...new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))];
}

function fillSeg(bar, ratio) {
  const on = Math.round(bar.children.length * Math.max(0, Math.min(1, ratio)));
  [...bar.children].forEach((cell, index) => cell.classList.toggle("on", index < on));
}

function control(field, value, onChange, { disabled = false, id } = {}) {
  const common = { id, disabled };
  let current = value;
  switch (field.type) {
    case "boolean": {
      const button = el("button.switch", { ...common, type: "button", role: "switch", "aria-checked": String(Boolean(current)), class: `switch${current ? " on" : ""}`, "aria-label": field.label });
      button.addEventListener("click", () => {
        current = !current;
        button.classList.toggle("on", current);
        button.setAttribute("aria-checked", String(current));
        onChange(current);
      });
      return button;
    }
    case "select": {
      const group = el("div.segmented", { role: "radiogroup", "aria-label": field.label }, el("span.seg-pill", { "aria-hidden": "true" }));
      field.options.forEach(([key, label]) => {
        const option = el("button", { type: "button", role: "radio", "aria-checked": String(current === key), class: current === key ? "on" : "", disabled, text: label, dataset: { value: key } });
        option.addEventListener("click", () => {
          if (current === key) return;
          current = key;
          $$(":scope > button", group).forEach((item) => {
            const on = item.dataset.value === key;
            item.classList.toggle("on", on);
            item.setAttribute("aria-checked", String(on));
          });
          slide(group);
          onChange(key);
        });
        group.append(option);
      });
      requestAnimationFrame(() => slide(group, true));
      return group;
    }
    case "number": {
      const input = el("input.input.num", { ...common, type: "number", min: field.min, max: field.max, step: "1", value: String(current ?? "") });
      const ranged = field.max - field.min <= 200;
      const bar = ranged ? segBar(24, ((current ?? field.min) - field.min) / (field.max - field.min || 1)) : null;
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
        current = number;
        if (bar) fillSeg(bar, (number - field.min) / (field.max - field.min || 1));
        onChange(number);
      });
      return ranged ? el("div.seg-input", {}, el("div.row", {}, bar, input)) : input;
    }
    case "list":
    case "textarea": {
      const area = el("textarea.input.area", { ...common, rows: field.type === "list" ? 3 : 4, spellcheck: "false" });
      area.value = field.type === "list" ? (current || []).join("\n") : current || "";
      area.addEventListener("change", () => onChange(field.type === "list" ? parseLines(area.value) : area.value));
      return area;
    }
    case "roles": {
      const group = el("div.role-chips", { role: "group", "aria-label": field.label });
      ROLE_OPTIONS.forEach(([key, label]) => {
        const has = (current || []).includes(key);
        const chip = el("button.role-chip", { type: "button", "aria-pressed": String(has), class: `role-chip${has ? " on" : ""}`, disabled, text: label });
        chip.addEventListener("click", () => {
          const on = current.includes(key);
          const next = on ? current.filter((role) => role !== key) : [...current, key];
          if (!next.length) {
            chip.classList.remove("err");
            void chip.offsetWidth;
            chip.classList.add("err");
            toast("至少保留一个角色", "info");
            return;
          }
          current = next;
          chip.classList.toggle("on", !on);
          chip.setAttribute("aria-pressed", String(!on));
          onChange(next);
        });
        group.append(chip);
      });
      return group;
    }
    case "aliases":
      return aliasEditor(current || {}, onChange);
    default: {
      const input = el("input.input", { ...common, type: "text", value: current ?? "" });
      input.addEventListener("change", () => onChange(input.value));
      return input;
    }
  }
}

function aliasEditor(value, onChange) {
  const rows = Object.entries(value).flatMap(([group, targets]) =>
    Object.entries(targets).map(([user, aliases]) => ({ group, user, aliases: aliases.join("，") })));
  const list = el("div.alias-list");
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
  const rowNode = (row) => {
    const input = (key, label, placeholder, numeric) => {
      const node = el("input.input", { type: "text", value: row[key], placeholder, "aria-label": label, inputmode: numeric ? "numeric" : null });
      node.addEventListener("change", () => { row[key] = node.value; commit(); });
      return node;
    };
    const node = el("div.alias-row", {},
      input("group", "群号", "群号", true),
      input("user", "QQ", "QQ", true),
      input("aliases", "别名", "别名，用逗号分隔", false));
    node.append(el("button.icon-btn", {
      type: "button",
      "aria-label": "删除这一行",
      onclick: () => {
        rows.splice(rows.indexOf(row), 1);
        leave(node);
        commit();
      },
    }, icon("x")));
    return node;
  };
  list.append(...rows.map(rowNode));
  const add = el("button.btn.btn-ghost.btn-sm", {
    type: "button",
    onclick: () => {
      const row = { group: state.overrideGroup || "", user: "", aliases: "" };
      rows.push(row);
      const node = rowNode(row);
      list.append(node);
      $("input", node).focus();
    },
  }, "添加别名");
  return el("div.alias-editor", {}, list, add);
}

/** 元素先淡出、再收起高度后移除。 */
function leave(node, done) {
  if (reducedMotion()) {
    node.remove();
    done?.();
    return;
  }
  node.style.height = `${node.offsetHeight}px`;
  node.classList.add("leaving");
  setTimeout(() => {
    node.classList.add("collapsing");
    node.style.height = "0px";
    setTimeout(() => {
      node.remove();
      done?.();
    }, 200);
  }, 160);
}

/** 滑动选中指示块：用于分区标签与分段切换。 */
function slide(container, instant = false) {
  if (!container) return;
  const pill = $(":scope > .seg-pill, :scope > .tab-pill", container);
  const active = $(":scope > .on", container);
  if (!pill) return;
  if (!active || !active.offsetWidth) {
    pill.style.opacity = "0";
    return;
  }
  if (instant || reducedMotion()) pill.style.transition = "none";
  Object.assign(pill.style, {
    opacity: "1",
    width: `${active.offsetWidth}px`,
    height: `${active.offsetHeight}px`,
    transform: `translate(${active.offsetLeft}px, ${active.offsetTop}px)`,
  });
  if (instant) requestAnimationFrame(() => { pill.style.transition = ""; });
}

function formRow(field, isChanged, controlNode, extra) {
  const id = `cfg-${field.key}`;
  return el("div.form-row", { class: `form-row${isChanged ? " changed" : ""}`, dataset: { key: field.key } },
    el("div.lbl", {}, el("label", { for: id }, el("strong", { text: field.label })), extra),
    el("div", {}, controlNode, field.hint ? el("div.help", { text: field.hint }) : null));
}

function flash(row) {
  if (!row || reducedMotion()) return;
  row.classList.remove("flash");
  void row.offsetWidth;
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 320);
}

/** 一次修改后的局部刷新：所在行的“已修改”、分区圆点、右侧差异与悬浮按钮。 */
function changed(key, changedNow) {
  const row = $(`#config-body .form-row[data-key="${CSS.escape(key)}"]`);
  if (row) {
    row.classList.toggle("changed", changedNow);
    flash(row);
  }
  updateTabDots();
  renderDraft();
}

// ── 分区标签与范围 ───────────────────────────

function renderTabs() {
  const tabs = $("#anchors");
  const sections = visibleSections();
  if (!sections.some((section) => section.id === state.section)) state.section = sections[0].id;
  tabs.replaceChildren(el("span.tab-pill", { "aria-hidden": "true" }), ...sections.map((section) => el("button", {
    type: "button",
    role: "tab",
    id: `tab-${section.id}`,
    class: state.section === section.id ? "on" : "",
    "aria-selected": String(state.section === section.id),
    "aria-controls": "config-body",
    tabindex: state.section === section.id ? "0" : "-1",
    dataset: { section: section.id },
  }, section.title, el("span.mod", { "aria-hidden": "true", hidden: !sectionDirty(section.id) }))));
  requestAnimationFrame(() => slide(tabs, true));
}

function updateTabDots() {
  $$("#anchors [data-section]").forEach((tab) => {
    const dirty = sectionDirty(tab.dataset.section);
    const dot = $(".mod", tab);
    if (dot.hidden === !dirty) return;
    dot.hidden = !dirty;
    if (dirty && !reducedMotion()) {
      dot.animate([{ transform: "scale(.4)", opacity: 0 }, { transform: "scale(1)", opacity: 1 }], { duration: 200, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
  });
}

function selectSection(id, { focus = false } = {}) {
  if (id === state.section) return;
  const order = visibleSections().map((section) => section.id);
  const direction = order.indexOf(id) > order.indexOf(state.section) ? 1 : -1;
  state.section = id;
  $$("#anchors [data-section]").forEach((tab) => {
    const on = tab.dataset.section === id;
    tab.classList.toggle("on", on);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    if (on && focus) tab.focus();
  });
  slide($("#anchors"));
  renderBody(direction);
}

function sectionCard(section, rows) {
  return el("section.card.card-pad.cfg-section", { id: `sec-${section.id}`, role: "tabpanel", "aria-labelledby": `tab-${section.id}` },
    el("div.card-title", {}, el("div", {}, el("h3", { text: section.title }), el("p", { text: section.description }))),
    rows);
}

function renderGlobal() {
  const section = SECTIONS.find((item) => item.id === state.section);
  return [sectionCard(section, section.fields.map((field) => formRow(
    field,
    !same(state.saved[field.key], state.draft[field.key]),
    control(field, state.draft[field.key], (value) => {
      state.draft[field.key] = value;
      changed(field.key, !same(state.saved[field.key], value));
    }, { id: `cfg-${field.key}` }),
  )))];
}

function overrideRow(field) {
  const group = state.overrideGroup;
  const overrides = state.draft.group_overrides || (state.draft.group_overrides = {});
  const current = () => overrides[group] || {};
  const savedValue = () => state.saved.group_overrides?.[group]?.[field.key];
  const enabled = Object.hasOwn(current(), field.key);
  const toggle = el("button.switch.switch-sm", {
    type: "button", role: "switch", "aria-checked": String(enabled), class: `switch switch-sm${enabled ? " on" : ""}`,
    "aria-label": `覆盖${field.label}`,
  });
  const node = control(field, enabled ? current()[field.key] : state.draft[field.key], (value) => {
    overrides[group] = { ...current(), [field.key]: value };
    changed(field.key, !same(value, savedValue()));
  }, { disabled: !enabled, id: `cfg-${field.key}` });
  const row = formRow(field, !same(current()[field.key], savedValue()), node,
    el("span.override-toggle", {}, toggle, enabled ? "覆盖中" : "继承全局"));
  toggle.addEventListener("click", () => {
    const next = { ...current() };
    if (Object.hasOwn(next, field.key)) delete next[field.key];
    else next[field.key] = clone(state.draft[field.key]);
    if (Object.keys(next).length) overrides[group] = next; else delete overrides[group];
    toggle.classList.toggle("on");
    toggle.setAttribute("aria-checked", String(Object.hasOwn(next, field.key)));
    // 开关动画结束后再按新的可编辑状态重建这一行。
    setTimeout(() => {
      const fresh = overrideRow(field);
      row.replaceWith(fresh);
      flash(fresh);
      $(".switch-sm", fresh).focus();
    }, reducedMotion() ? 0 : 200);
    updateOverrideSummary();
    updateTabDots();
    renderDraft();
  });
  return row;
}

function updateOverrideSummary() {
  const count = Object.keys(state.draft.group_overrides?.[state.overrideGroup] || {}).length;
  const summary = $("#override-summary");
  if (summary && state.overrideGroup) summary.textContent = `群 ${state.overrideGroup} · ${count} 个字段覆盖中，其余继承全局配置。`;
  const clear = $("#override-clear");
  if (clear) clear.disabled = !count;
}

function renderOverride() {
  const known = [...new Set([...state.groups(), ...Object.keys(state.draft.group_overrides || {})])].sort();
  const select = el("select.input", { id: "override-select", "aria-label": "选择群聊" },
    el("option", { value: "", text: known.length ? "选择群聊" : "暂无群聊" }),
    known.map((id) => el("option", { value: id, text: `${id}${state.draft.group_overrides?.[id] ? " · 已覆盖" : ""}` })));
  select.value = state.overrideGroup;
  select.addEventListener("change", () => openGroup(select.value));
  const input = el("input.input", { type: "text", inputmode: "numeric", placeholder: "或输入群号", "aria-label": "输入群号" });
  const open = el("button.btn.btn-ghost", { type: "button", text: "打开" });
  open.addEventListener("click", () => {
    const value = input.value.trim();
    if (!/^\d+$/.test(value) || value === "0") {
      input.classList.remove("err");
      void input.offsetWidth;
      input.classList.add("err");
      toast("群号必须是正整数", "bad");
      return;
    }
    openGroup(value);
  });
  const clear = el("button.btn.btn-danger.btn-sm", { id: "override-clear", type: "button", text: "清空本群覆盖" });
  clear.addEventListener("click", async () => {
    const group = state.overrideGroup;
    const count = Object.keys(state.draft.group_overrides?.[group] || {}).length;
    if (!await confirmAction({ title: "清空群聊覆盖", message: `将从草稿中移除群 ${group} 的 ${count} 个覆盖字段，保存后生效。`, confirm: "清空覆盖" })) return;
    delete state.draft.group_overrides[group];
    refresh();
  });
  const picker = el("div.card.card-pad", {},
    el("div.card-title", {},
      el("div", {}, el("h3", { text: "群聊覆盖" }),
        el("p", { id: "override-summary", text: "只覆盖打开开关的字段，其余继续继承全局配置。" })),
      state.overrideGroup ? clear : null),
    el("div.picker", {}, el("label.field", {}, "群聊", select), el("label.field", {}, "群号", input), open));
  if (!state.overrideGroup) return [picker];
  const section = SECTIONS.find((item) => item.id === state.section);
  const fields = section.fields.filter((field) => OVERRIDE_KEYS.includes(field.key));
  return [picker, sectionCard(section, fields.map(overrideRow))];
}

function openGroup(group) {
  state.overrideGroup = group;
  renderScope();
  renderBody();
  updateOverrideSummary();
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
      area.classList.remove("err");
      void area.offsetWidth;
      area.classList.add("err");
      return;
    }
    error.hidden = true;
    area.classList.remove("err");
    const next = clone(state.draft);
    for (const key of Object.keys(next)) if (Object.hasOwn(parsed, key)) next[key] = parsed[key];
    state.draft = next;
    renderDraft();
    toast("已应用到草稿，保存前仍可继续修改");
  });
  const format = el("button.btn.btn-ghost", { type: "button", text: "重新生成" });
  format.addEventListener("click", () => { area.value = JSON.stringify(state.draft, null, 2); error.hidden = true; });
  return [el("section.card.card-pad", {},
    el("div.card-title", {}, el("div", {}, el("h3", { text: "高级 JSON" }), el("p", { text: "批量粘贴完整配置；应用只更新草稿，最终由后端校验。" }))),
    error, area, el("div.inline-actions", {}, apply, format))];
}

/** 渲染当前范围与分区；direction 决定切换分区时内容从哪一侧进入。 */
function renderBody(direction = 0) {
  const body = $("#config-body");
  body.replaceChildren(...(state.scope === "global" ? renderGlobal() : state.scope === "override" ? renderOverride() : renderJson()));
  body.classList.remove("swap-left", "swap-right", "swap-up");
  void body.offsetWidth;
  body.classList.add(direction > 0 ? "swap-right" : direction < 0 ? "swap-left" : "swap-up");
  hydrate(body);
}

function renderScope() {
  $$("#scope > button").forEach((button) => {
    const on = button.dataset.scope === state.scope;
    button.classList.toggle("on", on);
    button.setAttribute("aria-checked", String(on));
  });
  slide($("#scope"));
  const tabs = $("#anchors");
  tabs.hidden = state.scope === "json" || (state.scope === "override" && !state.overrideGroup);
  if (!tabs.hidden) renderTabs();
}

// ── 右侧差异：按键复用列表项，只让新增项淡入、移除项淡出 ──

function renderDraft() {
  const list = changes();
  const card = $("#config-side");
  let body = $(".side-body", card);
  if (!list.length) {
    if (!body?.classList.contains("empty-draft")) {
      card.replaceChildren(el("div.side-body.empty-draft", {}, el("h3", { text: "没有未保存的修改" }),
        el("p.muted", { text: "修改左侧任意配置后，改动会列在这里，右下角按钮变为保存入口。保存成功后立即生效。" })));
    }
    state.onChange(0);
    return;
  }
  if (!body || body.classList.contains("empty-draft")) {
    body = el("div.side-body.draft", {}, el("h3"), el("ul.diff"));
    card.replaceChildren(body);
  }
  $("h3", body).textContent = `${list.length} 项改动待保存`;
  const ul = $("ul", body);
  const existing = new Map($$(":scope > li:not(.leaving)", ul).map((li) => [li.dataset.key, li]));
  let previous = null;
  for (const item of list) {
    let li = existing.get(item.key);
    existing.delete(item.key);
    if (!li) {
      li = el("li", { dataset: { key: item.key } }, el("span.name"), el("span.from"), el("span.to"));
      if (previous) previous.after(li); else ul.prepend(li);
    } else if ($(".to", li).textContent !== item.to && !reducedMotion()) {
      $(".to", li).animate([{ opacity: 0.2, transform: "translateY(3px)" }, { opacity: 1, transform: "none" }], { duration: 200, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
    $(".name", li).textContent = item.name;
    $(".from", li).textContent = item.from;
    $(".to", li).textContent = item.to;
    previous = li;
  }
  existing.forEach((li) => leave(li));
  state.onChange(list.length);
}

/** 草稿被整体替换后（放弃、保存、清空覆盖）重建当前视图。 */
function refresh() {
  renderScope();
  renderBody();
  renderDraft();
  if (state.scope === "override") updateOverrideSummary();
}

// ── 加载、放弃与保存 ─────────────────────────

export async function loadConfig() {
  const value = await bridge.apiGet("config");
  delete value.status;
  state.saved = clone(value);
  state.draft = clone(value);
  if ($("#config-body")) refresh();
}

export function discard() {
  state.draft = clone(state.saved);
  $("#save-error").hidden = true;
  refresh();
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
    refresh();
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
    if (!button || button.dataset.scope === state.scope) return;
    state.scope = button.dataset.scope;
    renderScope();
    renderBody();
    if (state.scope === "override") updateOverrideSummary();
  });
  $("#anchors").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-section]");
    if (tab) selectSection(tab.dataset.section);
  });
  $("#anchors").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const order = visibleSections().map((section) => section.id);
    const index = order.indexOf(state.section);
    const next = event.key === "Home" ? 0 : event.key === "End" ? order.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + order.length) % order.length;
    selectSection(order[next], { focus: true });
  });
  window.addEventListener("resize", () => {
    slide($("#anchors"), true);
    slide($("#scope"), true);
  });
  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  if (state.saved) refresh();
}

/** 切换到配置模块时重新对齐指示块（模块隐藏时无法测量尺寸）。 */
export function onShowConfig() {
  requestAnimationFrame(() => {
    slide($("#anchors"), true);
    slide($("#scope"), true);
    $$("#config-body .segmented").forEach((group) => slide(group, true));
  });
}
