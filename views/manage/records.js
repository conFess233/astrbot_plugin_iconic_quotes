// 记录模块：统计卡、筛选、列表、右侧页面卡片（普通 / 加宽 / 全屏）与删除。

import {
  $, $$, bridge, busy, confirmAction, countUp, el, formatBytes, formatTime, hydrate,
  icon, openLightbox, openSheet, reducedMotion, segBar, task, toast,
} from "./core.js";

const state = {
  stats: null,
  group: "",
  page: 1,
  pageSize: 20,
  total: 0,
  items: [],
  selected: null,
  checked: new Set(),
  wide: false,
  loaded: false,
  error: "",
};
const mediaCache = new Map();
const avatarCache = new Map();
const PALETTE = ["#4a5bd4", "#2c8f62", "#c77a1f", "#6a6d77", "#cf4040", "#7a5bd4"];

export const recordGroups = () => (state.stats?.groups || []).map((group) => group.group_id);

// ── 数据 ─────────────────────────────────────

export async function loadStats() {
  state.stats = await bridge.apiGet("stats");
  const groups = recordGroups();
  if (!groups.includes(state.group)) state.group = groups[0] || "";
  renderGroupOptions();
  renderStats();
  $("#header-status").replaceChildren(el("span.dot"), `已连接 · ${groups.length} 个群`);
  return state.stats;
}

export async function loadRecords({ animate = false } = {}) {
  const tbody = $("#rows");
  if (!state.group) {
    state.items = [];
    state.total = 0;
    state.loaded = true;
    renderRows();
    renderSide();
    return;
  }
  if (!state.loaded) renderSkeleton();
  try {
    const data = await bridge.apiGet("records", {
      group_id: state.group,
      page: state.page,
      page_size: state.pageSize,
      q: $("#search").value.trim(),
      type: $("#filter-type").value,
      has_image: $("#filter-image").value,
      health: $("#filter-health").value,
    });
    state.items = data.items || [];
    state.total = data.total || 0;
    state.error = "";
  } catch (error) {
    state.items = [];
    state.total = 0;
    state.error = error.message;
  }
  state.loaded = true;
  if (state.selected && !state.items.some((item) => item.id === state.selected)) {
    state.selected = null;
    setWide(false);
  }
  for (const id of [...state.checked]) {
    if (!state.items.some((item) => item.id === id)) state.checked.delete(id);
  }
  renderRows();
  renderSide();
  if (animate && !reducedMotion()) {
    $$("tr", tbody).forEach((row, index) => row.animate(
      [{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }],
      { duration: 220, delay: index * 24, easing: "cubic-bezier(.2,.8,.2,1)", fill: "backwards" },
    ));
  }
}

function mediaUrl(sha256) {
  if (!mediaCache.has(sha256)) {
    mediaCache.set(sha256, bridge.apiGet("media-data", { sha256 }).then((result) => result.data_url));
  }
  return mediaCache.get(sha256);
}

function avatarUrl(userId) {
  if (!avatarCache.has(userId)) {
    avatarCache.set(userId, bridge.apiGet("avatar-data", { user_id: userId }).then((result) => result.data_url));
  }
  return avatarCache.get(userId);
}

export function clearAvatarCache() {
  avatarCache.clear();
}

// ── 记录内容派生 ─────────────────────────────

function authorName(author) {
  return author?.nickname || author?.user_id || "未知用户";
}

/** 正文文字；图片在缩略图与“N 图”标签中单独展示，这里不重复占位。 */
function segmentText(segments) {
  return (segments || []).map((segment) => {
    if (segment.type === "text") return segment.text || "";
    if (segment.type === "face") return "[表情]";
    if (segment.type === "sticker") return segment.summary || "[商城表情]";
    return "";
  }).join("");
}

function* walkNodes(nodes) {
  for (const node of nodes || []) {
    yield node;
    for (const nested of node.nested_forwards || []) yield* walkNodes(nested.nodes);
  }
}

function* walkReplies(reply) {
  while (reply) {
    yield reply;
    reply = reply.reply;
  }
}

function mediaSegments(record) {
  const result = [];
  const take = (segments) => (segments || []).forEach((segment) => {
    if ((segment.type === "image" || segment.type === "sticker") && segment.sha256) result.push(segment);
  });
  take(record.segments);
  for (const reply of walkReplies(record.reply)) take(reply.segments);
  for (const node of walkNodes(record.nodes)) {
    take(node.segments);
    for (const reply of walkReplies(node.reply)) take(reply.segments);
  }
  return result;
}

function summary(record) {
  if (record.type === "forward") {
    const nodes = [...walkNodes(record.nodes)];
    const first = nodes.find((node) => (node.segments || []).length);
    return `合并转发 · ${nodes.length} 条${first ? `：${authorName(first.author)}：${segmentText(first.segments)}` : ""}`;
  }
  const text = segmentText(record.segments);
  return (record.reply ? "↩ " : "") + (text || "[图片]");
}

function displayAuthor(record) {
  if (record.type === "message") return record.author || {};
  const nodes = [...walkNodes(record.nodes)];
  const ids = new Set(nodes.map((node) => node.author?.user_id).filter(Boolean));
  return ids.size === 1 ? nodes[0].author : { nickname: `${ids.size || "多"} 位成员`, user_id: "" };
}

function status(record) {
  if (record.broken) return ["bad", "缺少媒体"];
  if (record.identity_incomplete) return ["warn", "身份待确认"];
  return ["ok", "正常"];
}

function avatar(author, size = "") {
  const name = authorName(author);
  const userId = author?.user_id || "";
  const color = PALETTE[[...userId].reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % PALETTE.length];
  const node = el(`span.avatar${size}`, { style: `background:${color}`, "aria-hidden": "true" }, name.slice(0, 1));
  if (userId) {
    avatarUrl(userId).then((url) => {
      if (url.startsWith("data:image/svg")) return;
      node.replaceChildren(el("img", { src: url, alt: "" }));
    }).catch(() => {});
  }
  return node;
}

// ── 统计卡 ───────────────────────────────────

function statCard(label, value, unit, extra, format) {
  const number = el("span");
  const card = el("div.stat", {},
    el("div.label", {}, el("span", { text: label })),
    el("div.value", {}, number, unit ? el("small", { text: unit }) : null),
    extra);
  countUp(number, value, format);
  return card;
}

function renderStats() {
  const stats = state.stats;
  const group = (stats?.groups || []).find((item) => item.group_id === state.group);
  const box = $("#stats");
  if (!stats) {
    box.replaceChildren();
    return;
  }
  const total = group?.total || 0;
  const limit = group?.limit || 1;
  const mediaRatio = stats.media_limit ? stats.media_bytes / stats.media_limit : 0;
  const broken = group?.broken || 0;
  box.replaceChildren(
    statCard("本群记录", total, `/ ${limit.toLocaleString("zh-CN")} 条`,
      segBar(20, total / limit, total / limit > 0.9 ? "warn" : "")),
    statCard("合并转发", group?.forwards || 0, "条",
      el("div.stat-sub", { text: `含图片记录 ${group?.with_images || 0} 条` })),
    statCard("异常记录", broken, "条",
      el("span", { class: `chip ${broken ? "bad" : "ok"}`, text: broken ? "需完整性检查" : "全部正常" })),
    statCard("媒体占用", stats.media_bytes, "", segBar(20, mediaRatio, mediaRatio > 0.8 ? "warn" : "ok"),
      (n) => formatBytes(n)),
  );
}

function renderGroupOptions() {
  const select = $("#group");
  const groups = recordGroups();
  select.replaceChildren(...(groups.length
    ? groups.map((id) => el("option", { value: id, text: id }))
    : [el("option", { value: "", text: "暂无群聊" })]));
  select.value = state.group;
  select.disabled = !groups.length;
}

// ── 列表 ─────────────────────────────────────

function renderSkeleton() {
  $("#rows").replaceChildren(...Array.from({ length: 6 }, (_, k) => el("tr.sk-row", { "aria-hidden": "true" },
    el("td", {}, el("span.sk", { style: "width:16px" })),
    el("td", {}, el("span.sk", { style: "width:96px" })),
    el("td", {}, el("span.sk", { style: `width:${55 + (k * 13) % 35}%` })),
    el("td.col-opt", {}, el("span.sk")),
    el("td.col-opt", {}, el("span.sk")),
    el("td.col-opt", {}, el("span.sk")))));
}

function emptyRow(message, retry) {
  return el("tr.empty-row", {}, el("td", { colspan: "6" },
    el("div.empty", {}, el("span", { text: message }),
      retry ? el("button.btn.btn-ghost.btn-sm", { type: "button", text: "重试", onclick: retry }) : null)));
}

function renderRows() {
  const tbody = $("#rows");
  const cards = $("#record-cards");
  const filtered = Boolean($("#search").value.trim() || $("#filter-type").value || $("#filter-image").value || $("#filter-health").value);
  let empty = null;
  if (state.error) empty = `加载失败：${state.error}`;
  else if (!state.group) empty = "还没有任何群典。在 QQ 群中引用消息发送 /添加群典 开始收录。";
  else if (!state.items.length) empty = filtered ? "没有符合筛选条件的记录。" : "该群还没有群典。";
  if (empty) {
    tbody.replaceChildren(emptyRow(empty, state.error ? () => task(() => loadRecords()) : null));
    cards.replaceChildren(el("div.card.empty", { text: empty }));
  } else {
    tbody.replaceChildren(...state.items.map(rowFor));
    cards.replaceChildren(...state.items.map(cardFor));
  }
  const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
  const end = Math.min(state.total, state.page * state.pageSize);
  $("#total").textContent = `${state.total} 条结果`;
  $("#range").textContent = `${start}–${end} / ${state.total}`;
  $("#page-label").textContent = `第 ${state.page} 页`;
  $("#prev").disabled = state.page <= 1;
  $("#next").disabled = end >= state.total;
  updateSelectionInfo();
}

function checkbox(record) {
  const on = state.checked.has(record.id);
  return el("button.cb", {
    type: "button",
    role: "checkbox",
    class: `cb${on ? " on" : ""}`,
    "aria-checked": String(on),
    "aria-label": `选择记录 ${record.id.slice(0, 8)}`,
    dataset: { check: record.id },
  });
}

function rowFor(record) {
  const author = displayAuthor(record);
  const [tone, label] = status(record);
  const images = mediaSegments(record).length;
  return el("tr", { class: state.selected === record.id ? "sel" : "", dataset: { id: record.id }, tabindex: "0" },
    el("td.check", {}, checkbox(record)),
    el("td", {}, el("div.who", {}, avatar(author),
      el("div", {}, el("span", { text: authorName(author) }), el("small", { text: author.user_id || "—" })))),
    el("td", {}, el("div.excerpt", {}, el("span", { text: summary(record) }),
      images ? el("span.chip.plain", { text: `${images} 图` }) : null)),
    el("td.col-opt", {}, el("span.chip.plain", { text: record.type === "forward" ? "合并转发" : "普通消息" })),
    el("td.col-opt.mono", { text: formatTime(record.recorded_at) }),
    el("td.col-opt", {}, el(`span.chip.${tone}`, { text: label })));
}

function cardFor(record) {
  const author = displayAuthor(record);
  const [tone, label] = status(record);
  return el("article.card.m-card", { class: `card m-card${state.selected === record.id ? " sel" : ""}`, dataset: { id: record.id }, tabindex: "0" },
    el("div.top", {}, el("div.who", {}, checkbox(record), avatar(author), el("span", { text: authorName(author) })),
      el(`span.chip.${tone}`, { text: label })),
    el("p", { text: summary(record) }),
    el("div.meta", {}, el("span.mono", { text: formatTime(record.recorded_at) }),
      el("span.chip.plain", { text: record.type === "forward" ? "合并转发" : "普通消息" })));
}

function updateSelectionInfo() {
  $("#selinfo").textContent = state.checked.size ? `已选 ${state.checked.size} 条` : "点击一行查看详情";
  $("#delete-selected").disabled = !state.checked.size;
  const pageIds = state.items.map((item) => item.id);
  const all = pageIds.length > 0 && pageIds.every((id) => state.checked.has(id));
  const header = $("#check-page");
  header.classList.toggle("on", all);
  header.setAttribute("aria-checked", String(all));
}

function toggleCheck(id) {
  if (state.checked.has(id)) state.checked.delete(id); else state.checked.add(id);
  $$(`[data-check="${CSS.escape(id)}"]`).forEach((node) => {
    node.classList.toggle("on", state.checked.has(id));
    node.setAttribute("aria-checked", String(state.checked.has(id)));
  });
  updateSelectionInfo();
}

function select(id) {
  state.selected = id;
  $$("#rows tr").forEach((row) => row.classList.toggle("sel", row.dataset.id === id));
  $$("#record-cards .m-card").forEach((card) => card.classList.toggle("sel", card.dataset.id === id));
  renderSide();
  if (innerWidth <= 1100) $("#side").scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}

// ── 右侧页面卡片 ─────────────────────────────

function setWide(wide) {
  state.wide = wide && innerWidth > 1100;
  $("#records-layout").classList.toggle("wide", state.wide);
}

function thumb(segment, index) {
  const image = el("img", { alt: `图片 ${index + 1}`, loading: "lazy" });
  const button = el("button.thumb", { type: "button", "aria-label": `查看图片 ${index + 1}`, dataset: { thumb: String(index) } }, image);
  mediaUrl(segment.sha256).then((url) => { image.src = url; }).catch(() => {
    button.classList.add("missing");
    button.disabled = true;
    button.replaceChildren(el("span", { text: "图片缺失" }));
  });
  return button;
}

function gallery(record, limit) {
  const media = mediaSegments(record);
  if (!media.length) return null;
  const shown = media.slice(0, limit);
  const grid = el("div.gallery", {}, shown.map(thumb));
  if (media.length > limit) {
    grid.append(el("button.thumb.more-thumbs", { type: "button", dataset: { expand: "1" }, text: `+${media.length - limit}` }));
  }
  return el("div", {}, el("div.field-label", { text: `图片 · ${media.length} 张` }), grid);
}

function replyBlock(reply) {
  if (!reply) return null;
  const chain = [...walkReplies(reply)];
  return el("div.reply-snap", {}, chain.map((item) => el("div", {},
    el("b", { text: `回复 ${authorName(item.author)}` }),
    el("span", { text: segmentText(item.segments) || (item.truncated ? "更早的回复已省略" : "") }))));
}

function nodeBubble(record, node, path, depth) {
  const children = [];
  const nestedByPosition = new Map();
  (node.nested_forwards || []).forEach((nested, index) => {
    if (!nestedByPosition.has(nested.position)) nestedByPosition.set(nested.position, []);
    nestedByPosition.get(nested.position).push([nested, index]);
  });
  const text = segmentText(node.segments);
  const bubble = el("div.bubble", {},
    el("div.bubble-head", {},
      el("b", { text: authorName(node.author) }),
      el("button.chip-btn", {
        type: "button",
        "aria-label": `删除 ${authorName(node.author)} 的这条节点`,
        title: "删除此节点",
        dataset: { node: JSON.stringify(path) },
      }, icon("x"))),
    replyBlock(node.reply),
    text ? el("span", { text }) : null);
  for (const [, items] of [...nestedByPosition.entries()].sort((a, b) => a[0] - b[0])) {
    for (const [nested, index] of items) {
      bubble.append(el("div.nested", {},
        el("div.field-label", { text: `聊天记录 · ${nested.nodes.length} 条` }),
        thread(record, nested.nodes, [...path, index], depth + 1)));
    }
  }
  children.push(avatar(node.author), bubble);
  return el("div.msg", {}, children);
}

function thread(record, nodes, prefix = [], depth = 0) {
  return el("div.thread", {}, nodes.map((node, index) => nodeBubble(record, node, [...prefix, index], depth)));
}

function quoteBlock(record) {
  return el("div.quote", {},
    el("div.who", {}, avatar(record.author),
      el("div", {}, el("span", { text: authorName(record.author) }), el("small", { text: record.author?.user_id || "—" }))),
    replyBlock(record.reply),
    el("p", { text: segmentText(record.segments) }));
}

function metaList(record) {
  const [tone, label] = status(record);
  return el("dl.kv", {},
    el("dt", { text: "群聊" }), el("dd", { text: record.group_id }),
    el("dt", { text: "类型" }), el("dd", { text: record.type === "forward" ? "合并转发" : "普通消息" }),
    el("dt", { text: "收录时间" }), el("dd", { text: formatTime(record.recorded_at, true) }),
    el("dt", { text: "收录者" }), el("dd", { text: authorName(record.recorded_by) }),
    el("dt", { text: "状态" }), el("dd", {}, el(`span.chip.${tone}`, { text: label })),
    el("dt", { text: "记录 ID" }), el("dd.mono", { text: record.id }));
}

function isBig(record) {
  const nodes = record.type === "forward" ? record.nodes.length : 0;
  return mediaSegments(record).length >= 4 || nodes > 3 || segmentText(record.segments).length > 300;
}

function overview() {
  const group = (state.stats?.groups || []).find((item) => item.group_id === state.group);
  if (!group) {
    return el("div.side-body", {}, el("h3", { text: "记录概览" }),
      el("p.muted", { text: "还没有群典数据。收录后可以在这里浏览、筛选和删除。" }));
  }
  return el("div.side-body", {},
    el("h3", { text: `群 ${group.group_id}` }),
    el("p.muted", { text: "点击左侧任意一行查看详情。图片较多或合并转发较长的记录可以加宽或全屏查看。" }),
    el("dl.kv", {},
      el("dt", { text: "普通金句" }), el("dd", { text: `${group.messages} 条` }),
      el("dt", { text: "合并转发" }), el("dd", { text: `${group.forwards} 条` }),
      el("dt", { text: "含图片" }), el("dd", { text: `${group.with_images} 条` }),
      el("dt", { text: "图片占用" }), el("dd", { text: formatBytes(group.image_bytes) }),
      el("dt", { text: "最早收录" }), el("dd", { text: formatTime(group.earliest) }),
      el("dt", { text: "最近收录" }), el("dd", { text: formatTime(group.latest) })));
}

function renderSide() {
  const side = $("#side");
  const record = state.items.find((item) => item.id === state.selected);
  if (!record) {
    side.replaceChildren(overview());
    return;
  }
  const wide = state.wide;
  const big = isBig(record);
  const nodes = record.type === "forward" ? record.nodes : [];
  const shownNodes = wide ? nodes : nodes.slice(0, 3);
  const main = record.type === "forward"
    ? el("div", {}, el("div.field-label", { text: `转发内容 · ${nodes.length} 条` }),
      thread(record, shownNodes),
      !wide && nodes.length > 3
        ? el("button.btn.btn-ghost.btn-sm.thread-more", { type: "button", dataset: { expand: "1" }, text: `展开其余 ${nodes.length - 3} 条` })
        : null)
    : quoteBlock(record);
  const canPreview = record.type === "message" && !record.reply
    && !(record.segments || []).some((segment) => segment.type === "face" || segment.type === "sticker");
  side.replaceChildren(el("div.side-body", {},
    el("div.side-head", {},
      el("span.chip.plain.mono", { text: `#${record.id.slice(0, 8)}` }),
      el("span.grow"),
      big || wide
        ? el("button.icon-btn.expand-btn", {
          type: "button", dataset: { expand: "1" }, "aria-pressed": String(wide),
          "aria-label": wide ? "收窄面板" : "加宽面板",
        }, icon(wide ? "right" : "left"))
        : null,
      el("button.icon-btn", { type: "button", dataset: { full: "1" }, "aria-label": "全屏查看" }, icon("maximize"))),
    el("h3", { text: record.type === "forward" ? "合并转发记录" : `${authorName(record.author)} 的群典` }),
    el("div.wide-cols", {}, el("div.stack", {}, main, wide ? metaList(record) : null), gallery(record, wide ? 15 : 5)),
    wide ? null : el("div.divider"),
    wide ? null : metaList(record),
    el("div.divider"),
    el("div.side-actions", {},
      canPreview ? el("button.btn.btn-tonal", { type: "button", dataset: { preview: "1" }, icon: "image" }, "预览卡片") : null,
      el("button.btn.btn-danger", { type: "button", dataset: { deleteOne: "1" }, icon: "trash" }, "删除记录"))));
}

function openFull(record) {
  const nodes = record.type === "forward" ? record.nodes : [];
  const left = el("div.stack", {},
    record.type === "forward"
      ? el("div", {}, el("div.field-label", { text: `转发内容 · ${nodes.length} 条` }), thread(record, nodes))
      : quoteBlock(record),
    metaList(record));
  const right = gallery(record, Infinity) || el("p.muted", { text: "这条记录没有图片。" });
  openSheet(record.type === "forward" ? "合并转发记录" : `${authorName(record.author)} 的群典`, left, right);
}

// ── 删除与预览 ───────────────────────────────

async function deleteRecords(ids) {
  if (!ids.length) return;
  const ok = await confirmAction({
    title: "删除群典",
    message: `将从群 ${state.group} 删除 ${ids.length} 条记录，同时清理不再被引用的图片。删除后无法恢复，可在审计中查看记录 ID。`,
    confirm: `删除 ${ids.length} 条`,
  });
  if (!ok) return;
  const button = $("#delete-selected");
  await busy(button, "删除中", async () => {
    try {
      const result = await bridge.apiPost("records/delete", { group_id: state.group, record_ids: ids });
      await animateRemoval(ids);
      ids.forEach((id) => state.checked.delete(id));
      if (ids.includes(state.selected)) {
        state.selected = null;
        setWide(false);
      }
      toast(`已删除 ${result.deleted} 条记录`);
      await loadStats();
      await loadRecords();
    } catch (error) {
      toast(`删除失败：${error.message}`, "bad");
    }
  });
}

function animateRemoval(ids) {
  const rows = ids.map((id) => $(`#rows tr[data-id="${CSS.escape(id)}"]`)).filter(Boolean);
  if (!rows.length || reducedMotion()) return Promise.resolve();
  rows.forEach((row) => row.classList.add("leaving"));
  return new Promise((resolve) => setTimeout(() => {
    rows.forEach((row) => row.classList.add("collapse"));
    setTimeout(resolve, 180);
  }, 200));
}

async function deleteNode(record, path) {
  const ok = await confirmAction({
    title: "删除转发节点",
    message: "将删除这条节点及其包含的子聊天记录。删空的子转发会一并移除；整条记录删空时记录也会被删除。",
    confirm: "删除节点",
  });
  if (!ok) return;
  await task(async () => {
    try {
      const result = await bridge.apiPost("records/nodes/delete", {
        group_id: record.group_id,
        record_id: record.id,
        content_hash: record.content_hash,
        node_path: path,
      });
      if (result.record_deleted) {
        state.selected = null;
        setWide(false);
        toast("节点已删除，记录已为空并被移除");
      } else {
        const index = state.items.findIndex((item) => item.id === record.id);
        if (index >= 0) state.items[index] = result.record;
        toast(`已删除 ${result.removed_nodes} 个节点`);
      }
      await loadStats();
      await loadRecords();
    } catch (error) {
      toast(`删除失败：${error.message}`, "bad");
    }
  });
}

async function previewCard(button, record) {
  await busy(button, "生成中", async () => {
    try {
      const result = await bridge.apiPost("preview", { group_id: record.group_id, record_id: record.id });
      const images = (result.images || []).map((url, index) => el("button.thumb.card-thumb", {
        type: "button", "aria-label": `查看卡片第 ${index + 1} 页`, dataset: { thumb: String(index) },
      }, el("img", { src: url, alt: `卡片预览第 ${index + 1} 页` })));
      openSheet("卡片预览 · 使用已保存配置",
        el("p.muted", { text: "卡片由 AstrBot 配置的文转图服务生成，实际发送效果与此一致。" }),
        el("div.gallery.card-gallery", {}, images));
    } catch (error) {
      toast(`预览失败：${error.message}`, "bad");
    }
  });
}

// ── 事件 ─────────────────────────────────────

export function focusSearch() {
  $("#search").focus();
  $("#search").select();
}

export function initRecords() {
  $("#group").addEventListener("change", () => {
    state.group = $("#group").value;
    state.page = 1;
    state.selected = null;
    state.checked.clear();
    setWide(false);
    renderStats();
    task(() => loadRecords({ animate: true }));
  });
  $("#filter-toggle").addEventListener("click", (event) => {
    const open = !$("#filters").classList.contains("open");
    $("#filters").classList.toggle("open", open);
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#filters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.page = 1;
    busy($("#query"), "查询中", () => loadRecords({ animate: true }));
  });
  $("#page-size").addEventListener("change", () => {
    state.pageSize = Number($("#page-size").value);
    state.page = 1;
    task(() => loadRecords());
  });
  $("#prev").addEventListener("click", () => { state.page -= 1; task(() => loadRecords({ animate: true })); });
  $("#next").addEventListener("click", () => { state.page += 1; task(() => loadRecords({ animate: true })); });
  $("#check-page").addEventListener("click", () => {
    const ids = state.items.map((item) => item.id);
    const all = ids.every((id) => state.checked.has(id));
    ids.forEach((id) => (all ? state.checked.delete(id) : state.checked.add(id)));
    renderRows();
  });
  $("#delete-selected").addEventListener("click", () => deleteRecords([...state.checked]));

  const listClick = (event) => {
    const check = event.target.closest("[data-check]");
    if (check) {
      event.stopPropagation();
      toggleCheck(check.dataset.check);
      return;
    }
    const row = event.target.closest("[data-id]");
    if (row) select(row.dataset.id);
  };
  const listKey = (event) => {
    const row = event.target.closest("[data-id]");
    if (!row || event.target.closest("[data-check]")) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select(row.dataset.id);
    }
  };
  for (const root of [$("#rows"), $("#record-cards")]) {
    root.addEventListener("click", listClick);
    root.addEventListener("keydown", listKey);
  }

  const detailClick = (event) => {
    const record = state.items.find((item) => item.id === state.selected);
    if (!record) return;
    const thumbButton = event.target.closest("[data-thumb]");
    if (thumbButton) {
      const group = $$("[data-thumb]:not(:disabled)", thumbButton.closest(".gallery"));
      openLightbox(group, group.indexOf(thumbButton));
      return;
    }
    if (event.target.closest("[data-expand]")) {
      if (innerWidth <= 1100) openFull(record);
      else {
        setWide(!state.wide);
        renderSide();
      }
      return;
    }
    if (event.target.closest("[data-full]")) return openFull(record);
    const nodeButton = event.target.closest("[data-node]");
    if (nodeButton) return deleteNode(record, JSON.parse(nodeButton.dataset.node));
    const previewButton = event.target.closest("[data-preview]");
    if (previewButton) return previewCard(previewButton, record);
    if (event.target.closest("[data-delete-one]")) deleteRecords([record.id]);
  };
  $("#side").addEventListener("click", detailClick);
  $("#sheet-body").addEventListener("click", (event) => {
    const thumbButton = event.target.closest("[data-thumb]");
    if (thumbButton) {
      const group = $$("[data-thumb]:not(:disabled)", thumbButton.closest(".gallery"));
      openLightbox(group, group.indexOf(thumbButton));
      return;
    }
    const record = state.items.find((item) => item.id === state.selected);
    const nodeButton = event.target.closest("[data-node]");
    if (record && nodeButton) deleteNode(record, JSON.parse(nodeButton.dataset.node));
  });
  window.addEventListener("resize", () => {
    if (innerWidth <= 1100 && state.wide) setWide(false);
  });
  hydrate($("#view-records"));
}
