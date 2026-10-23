import {
  h,
  clear,
  badge,
  emptyState,
  statCard,
  kv,
  button,
  textBlock,
  collapsible,
} from "./modules/dom.js";
import {
  fmtTime,
  fmtClock,
  fmtRelative,
  fmtNumber,
  fmtCompact,
  fmtTokens,
  fmtDuration,
  fmtMoney,
  fmtBytes,
  statusBadge,
  rangeToTs,
  dayLabel,
  hourLabel,
} from "./modules/format.js";
import { TrendChart } from "./modules/chart.js";
import { createApi, ApiError } from "./modules/api.js";
import { createToolboxUI } from "./modules/toolbox.js";
import { icon } from "./modules/icons.js";

const bridge = window.AstrBotPluginView;
const api = createApi(bridge);

const VIEW_META = {
  toolbox: { title: "工具箱" },
  settings: { title: "功能设置" },
  space: { title: "空间与日记" },
  quota: { title: "绘图额度" },
  overview: { title: "聊天总览" },
  groups: { title: "群聊" },
  privates: { title: "私聊" },
  requests: { title: "模型请求" },
  costs: { title: "费用与设置" },
};

const GROUP_LABELS = {
  persona_system: "人格/系统规则",
  history: "历史对话",
  current_message: "当前消息",
  temp_injection: "临时注入",
  tool_result: "工具结果",
  compression_summary: "压缩摘要",
  tool_call_history: "历史工具调用",
  checkpoint: "内部检查点",
  uncategorized: "未分类",
};

const state = {
  view: "toolbox",
  range: "24h",
  overview: null,
  health: null,
  sessions: [],
  sessionType: null,
  filterScopes: [],
  selectedScopeId: null,
  scopeSearch: "",
  sessionSort: "recent",
  scopeDetail: null,
  scopeContext: null,
  messages: [],
  replies: [],
  messageCursor: null,
  requestFilters: {
    status: "",
    scope_id: "",
    reasoning: "",
    cost_status: "",
    model: "",
  },
  requests: [],
  requestsCursor: null,
  costs: null,
  usage: null,
  prices: [],
  settings: null,
  drawer: {
    open: false,
    requestId: null,
    detail: null,
    tab: "input",
    content: {},
    loading: false,
  },
  refreshSeconds: 10,
  lastRefresh: null,
  chart: null,
  timer: null,
  busy: new Set(),
};

const $ = (id) => document.getElementById(id);
const toolbox = createToolboxUI({ api, toast, navigate: switchView });

// --------------------------------------------------------------------- boot
async function boot() {
  if (!bridge) {
    $("view-toolbox").appendChild(
      emptyState("无法连接 AstrBot", "请从 AstrBot 后台插件详情页打开此页面。"),
    );
    return;
  }
  await api.ready.catch((error) => {
    toast(error.message || "bridge 初始化失败", true);
  });
  bindChrome();
  bridge.onContext?.(() => {
    state.chart?.refresh();
    state.costChart?.refresh();
  });
  const meta = VIEW_META[state.view];
  $("view-title").textContent = meta.title;
  await renderView();
  startPolling();
  window.addEventListener("beforeunload", event => {
    if (toolbox.dirty) { event.preventDefault(); event.returnValue = ""; }
  });
}

function bindChrome() {
  $("open-settings-btn").addEventListener("click", () => switchView("settings"));
  document.querySelectorAll("#nav .nav-item").forEach((buttonEl) => {
    buttonEl.addEventListener("click", () => {
      const view = buttonEl.dataset.view;
      if (!view || view === state.view) return;
      switchView(view);
    });
  });
  document.querySelectorAll("#range-tabs button").forEach((buttonEl) => {
    buttonEl.addEventListener("click", async () => {
      const range = buttonEl.dataset.range;
      if (!range || range === state.range) return;
      state.range = range;
      document
        .querySelectorAll("#range-tabs button")
        .forEach((item) => item.classList.toggle("active", item === buttonEl));
      await refreshCurrent(true);
    });
  });
  $("refresh-btn").addEventListener("click", () => refreshCurrent(true));
  document.querySelectorAll("[data-close-drawer]").forEach((el) => {
    el.addEventListener("click", closeDrawer);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.drawer.open) closeDrawer();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      if (!toolbox.handles(state.view)) refreshHealth();
      refreshCurrent(false);
    }
  });
}

function switchView(view) {
  state.view = view;
  document
    .querySelectorAll("#nav .nav-item")
    .forEach((item) => {
      const current = item.dataset.view === view;
      item.classList.toggle("active", current);
      if (current) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  document.querySelectorAll(".view").forEach((section) => {
    section.classList.toggle("active", section.id === `view-${view}`);
  });
  const meta = VIEW_META[view] || VIEW_META.overview;
  $("view-title").textContent = meta.title;
  $("open-settings-btn").hidden = view === "settings";
  $("range-tabs").hidden = toolbox.handles(view);
  window.scrollTo(0, 0);
  renderView().catch(error => handleError(error));
}

async function renderView() {
  if (toolbox.handles(state.view)) {
    await toolbox.render(state.view);
    return;
  }
  await loadHealth();
  if (state.view === "overview") {
    await loadOverview();
    renderOverview();
    return;
  }
  if (state.view === "groups" || state.view === "privates") {
    await ensureSessions();
    renderSessionsView();
    return;
  }
  if (state.view === "requests") {
    if (!state.requests.length) await loadRequests(true);
    renderRequestsView();
    return;
  }
  if (state.view === "costs") {
    await ensureCosts();
    renderCostsView();
  }
}

// ------------------------------------------------------------------ polling
function startPolling() {
  if (state.timer) clearInterval(state.timer);
  const seconds = Math.max(0, Number(state.refreshSeconds) || 0);
  $("refresh-state").textContent = seconds ? `每 ${seconds}s` : "手动刷新";
  if (!seconds) return;
  state.timer = setInterval(async () => {
    if (document.hidden) return;
    if (!toolbox.handles(state.view)) await refreshHealth();
    if (state.drawer.open) return;
    await refreshCurrent(false);
  }, seconds * 1000);
}

async function refreshCurrent(showToast) {
  try {
    if (toolbox.handles(state.view)) {
      await toolbox.render(state.view, true, showToast);
    } else if (state.view === "overview") {
      await loadOverview();
      renderOverview();
    } else if (state.view === "groups" || state.view === "privates") {
      await ensureSessions(true);
      renderSessionsView();
      if (state.selectedScopeId) await loadSession(state.selectedScopeId, false);
    } else if (state.view === "requests") {
      await loadRequests(true);
      renderRequestsView();
    } else if (state.view === "costs") {
      await ensureCosts(true);
      renderCostsView();
    }
    state.lastRefresh = new Date();
    $("refresh-state").textContent = `已刷新 ${fmtClock(Date.now() / 1000)}`;
  } catch (error) {
    handleError(error, showToast);
  }
}

function handleError(error, loud = true) {
  console.error(error);
  if (error instanceof ApiError && (error.status === 401 || /登录/.test(error.message))) {
    toast("后台登录已失效，请重新登录 AstrBot 后台。", true);
    return;
  }
  if (loud) toast(error.message || "加载失败", true);
  const section = $(`view-${state.view}`);
  if (section && !section.childElementCount) {
    section.appendChild(emptyState("加载失败", error.message || "请稍后重试"));
  }
}

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), bad ? 4200 : 2200);
}

async function withBusy(key, fn) {
  if (state.busy.has(key)) return null;
  state.busy.add(key);
  try {
    return await fn();
  } finally {
    state.busy.delete(key);
  }
}

function rangeParams() {
  const { from, to } = rangeToTs(state.range);
  return { from, to };
}

// ------------------------------------------------------------------- health
async function loadHealth() {
  try {
    state.health = await api.get("health");
    const capture = state.health.capture || {};
    const dotEl = $("health-dot");
    const textEl = $("health-text");
    if (!capture.wrapper_in_place) {
      dotEl.className = "dot bad";
      textEl.textContent = "采集未生效";
    } else if ((state.health.collector?.queue_depth || 0) > 0) {
      dotEl.className = "dot warn";
      textEl.textContent = "正在写入";
    } else {
      dotEl.className = "dot ok";
      textEl.textContent = "采集运行中";
    }
  } catch (error) {
    $("health-dot").className = "dot bad";
    $("health-text").textContent = "健康检查失败";
  }
}

async function refreshHealth() {
  try {
    state.health = await api.get("health");
  } catch (error) {
    /* silent background check */
  }
}

// ----------------------------------------------------------------- overview
async function loadOverview() {
  const params = rangeParams();
  state.overview = await api.get("overview", params);
  if (state.health) state.overview.health = state.health;
}

function renderOverview() {
  const previousScroll = window.scrollY;
  const root = clear($("view-overview"));
  const data = state.overview;
  if (!data) {
    root.appendChild(emptyState("正在加载", "读取主聊天观察数据…"));
    return;
  }
  const cards = data.cards;
  const tokens = cards.tokens;
  const costEntries = Object.entries(cards.cost.by_currency || {});
  const costText = costEntries.length
    ? costEntries.map(([currency, amount]) => fmtMoney(amount, currency)).join(" · ")
    : "未配置单价";
  const costSub =
    cards.cost.estimated_requests > 0
      ? `${cards.cost.estimated_requests} 条已计价`
      : "未知单价不计为 0";
  const latency = cards.latency;

  root.appendChild(
    h(
      "div",
      { class: "stat-grid" },
      statCard({
        label: "回复轮数",
        value: fmtNumber(cards.reply_rounds),
        sub: "主聊天",
      }),
      statCard({
        label: "模型请求",
        value: fmtNumber(cards.requests.total),
        sub: `成功 ${fmtNumber(cards.requests.success)} · 失败 ${fmtNumber(cards.requests.failed)}`,
        tone: cards.requests.failed ? "amber" : "green",
      }),
      statCard({
        label: "输入 token",
        value: fmtCompact(tokens.input_other),
        sub: `缓存 ${fmtCompact(tokens.input_cached)} · 输出 ${fmtCompact(tokens.output)}`,
        tone: "violet",
      }),
      statCard({
        label: "估算费用",
        value: costText,
        sub: costSub,
        tone: costEntries.length ? "green" : "amber",
      }),
      statCard({
        label: "活跃会话",
        value: `${fmtNumber(cards.active_scopes.groups)} 群 · ${fmtNumber(cards.active_scopes.privates)} 私聊`,
        sub: "有直接请求",
      }),
      statCard({
        label: "平均延迟",
        value: latency.avg_ms !== null ? fmtDuration(latency.avg_ms) : "无样本",
        sub:
          latency.avg_ms !== null
            ? `p95 ${fmtDuration(latency.p95_ms)} · 首 token ${fmtDuration(latency.ttft_avg_ms)}`
            : "暂无延迟样本",
      }),
    ),
  );

  // Chart card.
  const chartCard = h("div", { class: "card" });
  chartCard.appendChild(
    h(
      "div",
      { class: "card-head" },
      h("h2", { text: "请求与 token 趋势" }),
      h("div", { class: "tiny muted", text: data.range.granularity === "hour" ? "按小时" : "按天" }),
    ),
  );
  const chartWrap = h("div", { class: "chart-wrap" });
  chartCard.appendChild(h("div", { class: "card-body" }, chartWrap));

  const trend = data.trend || [];
  const labels = trend.map((bucket) =>
    data.range.granularity === "hour" ? hourLabel(bucket.bucket_start) : dayLabel(bucket.bucket_start),
  );
  state.chart?.destroy();
  state.chart = new TrendChart(chartWrap);
  state.chart.setData({
    labels,
    series: [
      { name: "输入（非缓存）", values: trend.map((b) => b.input_other), color: "#b5533a", type: "bar" },
      { name: "缓存输入", values: trend.map((b) => b.input_cached), color: "#8c9c86", type: "bar" },
      { name: "输出", values: trend.map((b) => b.output), color: "#5b6c86", type: "bar" },
      { name: "请求数（右轴）", values: trend.map((b) => b.requests), color: "#9a7b3f", type: "line" },
    ],
  });

  // Top sessions card.
  const topCard = h("div", { class: "card" });
  topCard.appendChild(
    h("div", { class: "card-head" }, h("h2", { text: "用量最高的会话" })),
  );
  const topBody = h("div", { class: "card-body" });
  if (!data.top_sessions?.length) {
    topBody.appendChild(emptyState("暂无会话", "范围内还没有主聊天请求。"));
  } else {
    const table = h("table", { class: "data" });
    table.appendChild(
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { text: "会话" }),
          h("th", { class: "num", text: "请求" }),
          h("th", { class: "num", text: "token 合计" }),
          h("th", { text: "" }),
        ),
      ),
    );
    const tbody = h("tbody");
    for (const item of data.top_sessions) {
      const row = h(
        "tr",
        { class: "clickable" },
        h(
          "td",
          {},
          h("div", { class: "flex" }, badge(item.is_group ? "群" : "私聊", item.is_group ? "accent" : "violet"), h("strong", { text: item.display })),
        ),
        h("td", { class: "num", text: fmtNumber(item.requests) }),
        h("td", { class: "num", text: fmtCompact(item.tokens) }),
        h("td", { class: "right" }, h("span", { class: "tiny muted", text: "查看" })),
      );
      row.addEventListener("click", () => {
        state.selectedScopeId = item.scope_id;
        switchView(item.is_group ? "groups" : "privates");
      });
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    topBody.appendChild(table);
  }
  topCard.appendChild(topBody);

  // Coverage card.
  const nativeData = data.native_import || {};
  const coverage = data.coverage || {};
  const nativeCoverage = coverage.native?.platform_messages;
  const coverageCard = h("div", { class: "card" });
  coverageCard.appendChild(h("div", { class: "card-head" }, h("h2", { text: "统计口径" })));
  const coverageBody = h("div", { class: "card-body stack" });
  coverageBody.appendChild(
    h(
      "div",
      { class: "kv-grid" },
      kv("采集请求", fmtNumber(coverage.counts?.requests)),
      kv("回复轮次", fmtNumber(coverage.counts?.traces)),
      kv("平台消息", fmtNumber(coverage.counts?.messages)),
      kv("原生汇总", `${fmtNumber(nativeData.reply_rounds)} 轮`),
      kv("开始观察", fmtTime(coverage.live_started_at, true)),
      kv("导入消息", fmtNumber(nativeCoverage?.imported)),
    ),
  );
  if (coverage.recent_gaps?.length) {
    const gaps = h("div", { class: "note danger" });
    gaps.appendChild(h("div", { text: "采集缺口" }));
    for (const gap of coverage.recent_gaps.slice(0, 3)) {
      gaps.appendChild(
        h("div", { class: "tiny", text: `${fmtTime(gap.ts)} · ${gap.kind} · ${gap.detail || ""}` }),
      );
    }
    coverageBody.appendChild(gaps);
  }
  coverageBody.appendChild(
    h(
      "details",
      { class: "disclosure" },
      h("summary", { text: "说明" }),
      h("p", { text: "只统计 ToolLoopAgentRunner 的直接模型请求；日程、语音、绘图等辅助模型不计入。" }),
      h("p", { text: "原生 provider_stats 是每轮回复的汇总，可能与直接请求包含多次调用，不与直接请求相加。" }),
    ),
  );
  coverageCard.appendChild(coverageBody);

  root.appendChild(h("div", { class: "split-2" }, chartCard, topCard));
  root.appendChild(h("div", { class: "overview-coverage" }, coverageCard));
  window.scrollTo(0, previousScroll);
}

// ---------------------------------------------------------------- sessions
async function ensureSessions(force = false) {
  const type = state.view === "groups" ? "group" : "private";
  if (state.sessions.length && state.sessionType === type && !force) return;
  const params = {
    ...rangeParams(),
    type,
    sort: state.sessionSort,
    q: state.scopeSearch,
    limit: 200,
  };
  const data = await api.get("sessions", params);
  state.sessions = data.sessions || [];
  state.sessionType = type;
  if (
    state.selectedScopeId &&
    !state.sessions.some((item) => item.scope_id === state.selectedScopeId)
  ) {
    state.selectedScopeId = null;
  }
  if (!state.selectedScopeId && state.sessions.length) {
    state.selectedScopeId = state.sessions[0].scope_id;
  }
}

function renderSessionsView() {
  const section = $(`view-${state.view}`);
  const previousScroll = section?.querySelector(".chat-scroll")?.scrollTop || 0;
  const root = clear(section);
  const layout = h("div", { class: "session-layout" });
  const list = h("div", { class: "session-list" });
  const tools = h("div", { class: "session-tools" });
  const search = h("input", {
    class: "input",
    placeholder: state.view === "groups" ? "搜索群名或群号" : "搜索联系人或 QQ",
    value: state.scopeSearch,
  });
  let debounce = null;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(async () => {
      state.scopeSearch = search.value.trim();
      await ensureSessions(true);
      renderSessionsView();
    }, 260);
  });
  const sort = h("select", { class: "select" }, [
    h("option", { value: "recent", text: "最近活动" }),
    h("option", { value: "tokens", text: "token 用量" }),
    h("option", { value: "requests", text: "请求数" }),
  ]);
  sort.value = state.sessionSort;
  sort.addEventListener("change", async () => {
    state.sessionSort = sort.value;
    await ensureSessions(true);
    renderSessionsView();
  });
  tools.appendChild(search);
  tools.appendChild(h("div", { class: "row" }, sort));
  list.appendChild(tools);

  const scroll = h("div", { class: "session-scroll" });
  if (!state.sessions.length) {
    scroll.appendChild(emptyState("没有匹配的会话", "产生新的主聊天请求，或调整搜索条件。"));
  }
  for (const item of state.sessions) {
    const active = item.scope_id === state.selectedScopeId;
    const row = h(
      "div",
      { class: `session-item ${active ? "active" : ""}`.trim() },
      h("div", { class: `avatar ${item.is_group ? "" : "private"}`.trim() }, (item.display || "?").slice(0, 1)),
      h(
        "div",
        {},
        h("div", { class: "name" }, h("span", { class: "ellipsis", text: item.display }), item.is_group ? null : badge("私聊", "violet")),
        h("div", { class: "meta ellipsis", text: `${item.messages} 条 · ${item.requests} 请求 · ${fmtTokens(
          (item.tokens?.input_other || 0) + (item.tokens?.input_cached || 0) + (item.tokens?.output || 0),
        )}` }),
      ),
      h("div", { class: "right" }, [
        h("div", { text: fmtRelative(item.last_activity) }),
        item.failures ? h("div", { class: "down", text: `失败 ${item.failures}` }) : null,
      ]),
    );
    row.addEventListener("click", () => {
      state.selectedScopeId = item.scope_id;
      renderSessionsView();
      loadSession(item.scope_id, true);
    });
    scroll.appendChild(row);
  }
  list.appendChild(scroll);

  const pane = h("div", { class: "chat-pane" });
  if (!state.selectedScopeId) {
    pane.appendChild(emptyState("选择一个会话", "从左侧列表读取消息与上下文。"));
  } else if (!state.scopeDetail || state.scopeDetail.scope_id !== state.selectedScopeId) {
    pane.appendChild(emptyState("正在加载会话", "读取消息与上下文…"));
    loadSession(state.selectedScopeId, true);
  } else {
    pane.appendChild(renderChatPane());
  }
  layout.appendChild(list);
  layout.appendChild(pane);
  root.appendChild(layout);
  const nextScroll = root.querySelector(".chat-scroll");
  if (nextScroll) nextScroll.scrollTop = previousScroll;
}

async function loadSession(scopeId, reloadMessages = true) {
  if (!scopeId) return;
  const guard = `session:${scopeId}`;
  if (state.busy.has(guard)) return;
  state.busy.add(guard);
  try {
    const [detail, context] = await Promise.all([
      api.get(`sessions/${encodeURIComponent(scopeId)}`),
      api.get(`sessions/${encodeURIComponent(scopeId)}/context`),
    ]);
    state.scopeDetail = detail;
    state.scopeContext = context;
    if (reloadMessages || !state.messages.length) {
      await loadMessages(scopeId, null);
    }
    if (state.view === "groups" || state.view === "privates") {
      renderSessionsView();
    }
  } catch (error) {
    handleError(error, true);
  } finally {
    state.busy.delete(guard);
  }
}

async function loadMessages(scopeId, cursor) {
  const params = { limit: 50 };
  if (cursor) {
    params.before_ts = cursor.before_ts;
    params.before_id = cursor.before_id;
  }
  const data = await api.get(`sessions/${encodeURIComponent(scopeId)}/messages`, params);
  const messages = data.messages || [];
  if (cursor) {
    state.messages = [...messages, ...state.messages];
  } else {
    state.messages = messages;
    state.replies = data.replies || [];
  }
  state.messageCursor = data.next_cursor;
}

function renderChatPane() {
  const detail = state.scopeDetail;
  const context = state.scopeContext || {};
  const pane = h("div", { class: "chat-pane" });
  const head = h("div", { class: "chat-head" });
  head.appendChild(
    h(
      "div",
      { class: "line" },
      h("h2", { text: detail.display }),
      h(
        "div",
        { class: "pill-row" },
        badge(detail.is_group ? "群聊" : "私聊", detail.is_group ? "accent" : "violet"),
        badge(detail.platform_id || "unknown", "line"),
        button("刷新", async () => {
          await loadSession(detail.scope_id, true);
          toast("已刷新");
        }),
      ),
    ),
  );
  head.appendChild(
    h(
      "div",
      { class: "ids" },
      h("span", { text: detail.umo }),
      h("span", { text: `首见 ${fmtTime(detail.first_seen)}` }),
    ),
  );
  pane.appendChild(head);

  const occupancy = context.occupancy;
  const budget = context.budget || {};
  const trigger = budget.trigger_tokens;
  const used = occupancy?.tokens;
  const pct = used && trigger ? Math.min(100, (used / trigger) * 100) : 0;
  const strip = h("div", { class: "context-strip" });
  strip.appendChild(
    h(
      "div",
      { class: "context-item" },
      h("div", { class: "k", text: "输入量" }),
      h("div", { class: "v", text: used !== undefined && used !== null ? fmtTokens(used) : "暂无请求" }),
    ),
  );
  strip.appendChild(
    h(
      "div",
      { class: "context-item" },
      h("div", { class: "k", text: "压缩触发" }),
      h("div", { class: "v", text: trigger ? fmtTokens(trigger) : "未启用" }),
      h("div", { class: "progress" }, h("i", { style: { transform: `scaleX(${pct / 100})` } })),
    ),
  );
  strip.appendChild(
    h(
      "div",
      { class: "context-item" },
      h("div", { class: "k", text: "本轮用量" }),
      h(
        "div",
        { class: "v", text: context.latest_trace?.usage ? fmtCompact((context.latest_trace.usage.input_other || 0) + (context.latest_trace.usage.input_cached || 0) + (context.latest_trace.usage.output || 0)) : "—" },
      ),
    ),
  );
  strip.appendChild(
    h(
      "div",
      { class: "context-item" },
      h("div", { class: "k", text: "压缩事件" }),
      h("div", { class: "v", text: `${(context.compressions || []).length} 次 · ${budget.strategy || "—"}` }),
    ),
  );
  pane.appendChild(strip);

  const scroll = h("div", { class: "chat-scroll" });
  if (state.messageCursor) {
    scroll.appendChild(
      h("div", { class: "chat-load" }, button("加载更早的消息", async () => {
        await loadMessages(detail.scope_id, state.messageCursor);
        renderSessionsView();
      })),
    );
  }
  const items = mergeTimeline(state.messages, state.replies);
  if (!items.length) {
    scroll.appendChild(
      emptyState("暂无消息", "安装后的新消息会出现在这里。"),
    );
  }
  for (const item of items) {
    scroll.appendChild(renderChatItem(item));
  }
  pane.appendChild(scroll);
  return pane;
}

function mergeTimeline(messages, replies) {
  const items = [
    ...messages.map((item) => ({ ...item, _ts: item.ts })),
    ...replies.map((item) => ({ ...item, _ts: item.ts })),
  ];
  const seen = new Set();
  return items
    .filter((item) => {
      if (seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    })
    .sort((a, b) => (a._ts || 0) - (b._ts || 0));
}

function renderChatItem(item) {
  const direction = item.direction === "out" ? "out" : "in";
  const bubble = h("div", { class: "bubble" });
  const who = h("div", { class: "who" });
  who.appendChild(h("strong", { text: item.sender_name || (direction === "out" ? "bot" : "未知发送者") }));
  who.appendChild(h("span", { text: fmtClock(item.ts) }));
  if (item.kind === "reply_generated") {
    who.appendChild(badge("模型生成", "violet"));
  } else if (item.status) {
    const statusMap = statusBadge(item.status);
    who.appendChild(badge(statusMap.label, statusMap.kind));
  }
  bubble.appendChild(who);

  if (item.reply_to) {
    bubble.appendChild(h("div", { class: "quote", text: `引用消息 ${item.reply_to}` }));
  }
  const parts = item.parts || [];
  const attachments = parts.filter((part) => ["image", "record", "video", "file", "json", "forward"].includes(part.type));
  for (const part of attachments) {
    bubble.appendChild(
      h("span", {
        class: "attachment",
        text: `${part.type.toUpperCase()}${part.name ? ` · ${part.name}` : ""}${part.file_present ? " · 本地" : ""}`,
        title: part.url || "",
      }),
    );
  }
  if (item.text) {
    bubble.appendChild(textBlock(item.text));
  } else if (!attachments.length) {
    bubble.appendChild(h("div", { class: "text muted", text: "（非文本消息）" }));
  }
  const foot = h("div", { class: "foot" });
  if (item.source === "native_pmh") foot.appendChild(h("span", { text: "原生记录" }));
  if (item.trace_id) {
    foot.appendChild(
      h("button", {
        class: "btn ghost small",
        text: "请求详情",
        onclick: () => openTrace(item.trace_id),
      }),
    );
  }
  bubble.appendChild(foot);

  return h("div", { class: `msg ${direction} ${item.kind === "reply_generated" ? "msg-synthetic" : ""}`.trim() }, bubble);
}

// ----------------------------------------------------------------- requests
async function loadRequests(reset = false) {
  const params = {
    ...rangeParams(),
    limit: 50,
    ...Object.fromEntries(Object.entries(state.requestFilters).filter(([, value]) => value)),
  };
  if (!reset && state.requestsCursor) params.before = state.requestsCursor;
  const data = await api.get("requests", params);
  state.requests = reset ? data.requests || [] : [...state.requests, ...(data.requests || [])];
  state.requestsCursor = data.next_cursor;
}

async function ensureScopesForFilter() {
  if (state.filterScopes.length) return;
  try {
    const data = await api.get("sessions", { ...rangeParams(), type: "all", limit: 200 });
    state.filterScopes = data.sessions || [];
  } catch {
    /* optional */
  }
}

function renderRequestsView() {
  const previousScroll = window.scrollY;
  const root = clear($("view-requests"));
  ensureScopesForFilter().then(() => {
    if (
      state.view === "requests" &&
      state.filterScopes.length &&
      !document.querySelector("#request-scope-filter option[data-scope]")
    ) {
      renderRequestsView();
    }
  });

  const toolbar = h("div", { class: "toolbar" });
  const statusSelect = h("select", { class: "select", style: { width: "150px" } }, [
    h("option", { value: "", text: "全部状态" }),
    h("option", { value: "completed", text: "已完成" }),
    h("option", { value: "error", text: "失败" }),
    h("option", { value: "cancelled", text: "已取消" }),
    h("option", { value: "incomplete", text: "不完整" }),
  ]);
  statusSelect.value = state.requestFilters.status;
  statusSelect.addEventListener("change", async () => {
    state.requestFilters.status = statusSelect.value;
    await loadRequests(true);
    renderRequestsView();
  });
  const scopeSelect = h("select", { class: "select", id: "request-scope-filter", style: { width: "190px" } }, [
    h("option", { value: "", text: "全部会话" }),
    ...state.filterScopes.map((item) =>
      h("option", { value: item.scope_id, text: item.display, "data-scope": "1" }),
    ),
  ]);
  scopeSelect.value = state.requestFilters.scope_id;
  scopeSelect.addEventListener("change", async () => {
    state.requestFilters.scope_id = scopeSelect.value;
    await loadRequests(true);
    renderRequestsView();
  });
  const reasoningSelect = h("select", { class: "select", style: { width: "140px" } }, [
    h("option", { value: "", text: "思考：全部" }),
    h("option", { value: "available", text: "有思考" }),
    h("option", { value: "not_provided", text: "未提供" }),
    h("option", { value: "problem", text: "部分/过期" }),
  ]);
  reasoningSelect.value = state.requestFilters.reasoning;
  reasoningSelect.addEventListener("change", async () => {
    state.requestFilters.reasoning = reasoningSelect.value;
    await loadRequests(true);
    renderRequestsView();
  });
  const costSelect = h("select", { class: "select", style: { width: "160px" } }, [
    h("option", { value: "", text: "费用：全部" }),
    h("option", { value: "estimated", text: "已估算" }),
    h("option", { value: "price_not_configured", text: "单价未配置" }),
    h("option", { value: "unknown_usage", text: "用量未知" }),
    h("option", { value: "incomplete_price", text: "单价不完整" }),
  ]);
  costSelect.value = state.requestFilters.cost_status;
  costSelect.addEventListener("change", async () => {
    state.requestFilters.cost_status = costSelect.value;
    await loadRequests(true);
    renderRequestsView();
  });
  const modelInput = h("input", {
    class: "input",
    style: { width: "180px" },
    placeholder: "模型名",
    value: state.requestFilters.model,
  });
  modelInput.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") return;
    state.requestFilters.model = modelInput.value.trim();
    await loadRequests(true);
    renderRequestsView();
  });
  toolbar.appendChild(statusSelect);
  toolbar.appendChild(scopeSelect);
  toolbar.appendChild(reasoningSelect);
  toolbar.appendChild(costSelect);
  toolbar.appendChild(modelInput);
  toolbar.appendChild(h("div", { class: "spacer" }));
  root.appendChild(toolbar);

  const card = h("div", { class: "card" });
  card.appendChild(
    h(
      "div",
      { class: "card-head" },
      h("h2", { text: "请求列表" }),
      h("span", { class: "tiny muted", text: `${state.requests.length} 条` }),
    ),
  );
  const body = h("div", { class: "card-body" });
  if (!state.requests.length) {
    body.appendChild(
      emptyState(
        "暂无请求",
        "产生一次新的机器人回复后，这里会出现记录。",
      ),
    );
  } else {
    const table = h("table", { class: "data" });
    table.appendChild(
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { text: "时间" }),
          h("th", { text: "会话" }),
          h("th", { text: "步骤/尝试" }),
          h("th", { text: "模型" }),
          h("th", { text: "状态" }),
          h("th", { class: "num", text: "输入" }),
          h("th", { class: "num", text: "输出" }),
          h("th", { class: "num", text: "延迟" }),
          h("th", { text: "公开思考" }),
          h("th", { text: "费用" }),
          h("th", { text: "回复预览" }),
        ),
      ),
    );
    const tbody = h("tbody");
    for (const row of state.requests) {
      const status = statusBadge(row.status);
      const reasoning = statusBadge(row.reasoning_status);
      const cost = row.cost?.status ? statusBadge(row.cost.status) : null;
      const tr = h(
        "tr",
        { class: "clickable" },
        h("td", { class: "nowrap" }, h("div", { text: fmtTime(row.started_at) }), h("div", { class: "tiny muted", text: fmtRelative(row.started_at) })),
        h("td", {}, h("div", { class: "ellipsis", style: { maxWidth: "180px" }, text: row.display }), h("div", { class: "tiny muted", text: row.is_group ? "群聊" : "私聊" })),
        h("td", { class: "nowrap", text: `${row.step_no}/${row.attempt_no}${row.is_fallback ? " · 回退" : ""}` }),
        h("td", {}, h("div", { class: "ellipsis", style: { maxWidth: "190px" }, text: row.provider_model || "—" }), h("div", { class: "tiny muted", text: row.provider_id || "" })),
        h("td", {}, badge(status.label, status.kind)),
        h("td", { class: "num", text: fmtTokens(row.usage?.input_other) }),
        h("td", { class: "num", text: fmtTokens(row.usage?.output) }),
        h("td", { class: "num", text: fmtDuration(row.latency_ms) }),
        h("td", {}, badge(reasoning.label, reasoning.kind)),
        h("td", {}, cost ? badge(cost.label, cost.kind) : h("span", { class: "muted", text: "—" })),
        h("td", {}, h("div", { class: "ellipsis", style: { maxWidth: "260px" }, text: row.reply_preview || "（空）" })),
      );
      tr.addEventListener("click", () => openRequest(row.request_id));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    body.appendChild(h("div", { class: "table-wrap" }, table));
    if (state.requestsCursor) {
      body.appendChild(
        h("div", { style: { marginTop: "12px", textAlign: "center" } }, button("加载更多", async () => {
          await loadRequests(false);
          renderRequestsView();
        })),
      );
    }
  }
  card.appendChild(body);
  root.appendChild(card);
  window.scrollTo(0, previousScroll);
}

// ------------------------------------------------------------- costs/settings
async function ensureCosts(force = false) {
  if (state.costs && !force) return;
  const params = rangeParams();
  const [costs, usage, prices, settings, health, scopes] = await Promise.all([
    api.get("costs", { ...params, limit: 200 }),
    api.get("usage", { ...params, group_by: "day" }),
    api.get("prices"),
    api.get("settings"),
    api.get("health"),
    api.get("sessions", { ...rangeParams(), type: "all", limit: 200 }),
  ]);
  state.costs = costs;
  state.usage = usage;
  state.prices = prices.prices || [];
  state.settings = settings.settings || {};
  state.health = health;
  if (!state.filterScopes.length) state.filterScopes = scopes.sessions || [];
  if (state.settings.refresh_seconds !== undefined && state.settings.refresh_seconds !== state.refreshSeconds) {
    state.refreshSeconds = state.settings.refresh_seconds;
    startPolling();
  }
}

function renderCostsView() {
  const previousScroll = window.scrollY;
  const root = clear($("view-costs"));
  const costs = state.costs || { summary: [], rows: [], note: "" };
  const usage = state.usage || { buckets: [] };
  const settings = state.settings || {};

  const summary = costs.summary || [];
  const estimated = summary.filter((item) => item.status === "estimated");
  const unknown = summary.filter((item) => item.status !== "estimated");
  root.appendChild(
    h(
      "div",
      { class: "stat-grid" },
      statCard({
        label: "估算费用",
        value: estimated.length ? estimated.map((item) => fmtMoney(item.amount, item.currency)).join(" · ") : "未配置单价",
        sub: estimated.length ? `${estimated.reduce((sum, item) => sum + item.count, 0)} 条请求` : "未知单价不计为 0",
        tone: estimated.length ? "green" : "amber",
      }),
      statCard({
        label: "无法估算",
        value: fmtNumber(unknown.reduce((sum, item) => sum + item.count, 0)),
        sub: unknown.length
          ? unknown.map((item) => `${statusBadge(item.status).label} ${item.count}`).join(" · ")
          : "全部已计价",
        tone: "amber",
      }),
      statCard({
        label: "价格版本",
        value: fmtNumber(state.prices.length),
        sub: "只影响之后的新请求",
        tone: "violet",
      }),
      statCard({
        label: "正文数据",
        value: fmtBytes(state.health?.storage?.body_bytes),
        sub: `预算 ${settings.data_budget_gb ?? 2} GB · 过期 ${fmtNumber(state.health?.storage?.expired_body_count)}`,
      }),
    ),
  );

  const chartCard = h("div", { class: "card" });
  chartCard.appendChild(h("div", { class: "card-head" }, h("h2", { text: "每日 token 用量" })));
  const chartWrap = h("div", { class: "chart-wrap" });
  chartCard.appendChild(h("div", { class: "card-body" }, chartWrap));
  const buckets = (usage.buckets || []).slice().sort((a, b) => Number(a.key) - Number(b.key));
  state.costChart?.destroy();
  state.costChart = new TrendChart(chartWrap);
  state.costChart.setData({
    labels: buckets.map((item) => dayLabel(Number(item.key))),
    series: [
      { name: "输入（非缓存）", values: buckets.map((item) => item.input_other), type: "bar", color: "#b5533a" },
      { name: "缓存输入", values: buckets.map((item) => item.input_cached), type: "bar", color: "#8c9c86" },
      { name: "输出", values: buckets.map((item) => item.output), type: "bar", color: "#5b6c86" },
      { name: "请求数（右轴）", values: buckets.map((item) => item.requests), type: "line", color: "#9a7b3f" },
    ],
  });

  const costTableCard = h("div", { class: "card" });
  costTableCard.appendChild(
    h(
      "div",
      { class: "card-head" },
      h("h2", { text: "费用明细" }),
    ),
  );
  const costBody = h("div", { class: "card-body" });
  if (!costs.rows?.length) {
    costBody.appendChild(emptyState("暂无费用记录", "产生请求后按已配置单价估算。"));
  } else {
    const table = h("table", { class: "data" });
    table.appendChild(
      h("thead", {}, h("tr", {}, [
        "时间","会话","模型","输入/缓存/输出","单价状态","金额",
      ].map((label) => h("th", { text: label, class: label === "金额" ? "num" : "" })))),
    );
    const tbody = h("tbody");
    const scopeName = (scopeId) =>
      state.sessions.find((item) => item.scope_id === scopeId)?.display ||
      state.filterScopes.find((item) => item.scope_id === scopeId)?.display ||
      scopeId.slice(0, 8);
    for (const row of costs.rows.slice(0, 200)) {
      const status = statusBadge(row.status);
      tbody.appendChild(
        h("tr", { class: "clickable", onclick: () => openRequest(row.request_id) },
          h("td", { class: "nowrap", text: fmtTime(row.started_at) }),
          h("td", { text: scopeName(row.scope_id) }),
          h("td", { text: row.provider_model || "—" }),
          h("td", { class: "num", text: `${fmtCompact(row.usage?.input_other)} / ${fmtCompact(row.usage?.input_cached)} / ${fmtCompact(row.usage?.output)}` }),
          h("td", {}, badge(status.label, status.kind)),
          h("td", { class: "num", text: row.amount ? fmtMoney(row.amount, row.currency) : "—" }),
        ),
      );
    }
    table.appendChild(tbody);
    costBody.appendChild(h("div", { class: "table-wrap", style: { maxHeight: "420px" } }, table));
  }
  costTableCard.appendChild(costBody);

  // Price editor.
  const priceCard = h("div", { class: "card" });
  priceCard.appendChild(
    h(
      "div",
      { class: "card-head" },
      h("h2", { text: "单价版本" }),
      h("div", { class: "tiny muted", text: "每百万 token" }),
    ),
  );
  const priceBody = h("div", { class: "card-body stack" });
  const formGrid = h("div", { class: "form-grid" });
  const fields = {
    provider_id: { label: "Provider ID", value: "", placeholder: "选填" },
    model: { label: "模型", value: "*", placeholder: "* 通配" },
    tier_label: { label: "档位", value: "标准" },
    min_input_tokens: { label: "起算输入", value: "0" },
    currency: { label: "币种", value: "CNY" },
    billing_mode: { label: "计费", value: "per_token", select: [["per_token","按 token"],["subscription_or_quota","订阅/额度"],["unknown","未知"]] },
    input_price: { label: "输入", value: "" },
    cached_input_price: { label: "缓存输入", value: "" },
    output_price: { label: "输出", value: "" },
    note: { label: "备注", value: "" },
  };
  const inputs = {};
  for (const [key, spec] of Object.entries(fields)) {
    let control;
    if (spec.select) {
      control = h("select", { class: "select" }, spec.select.map(([value, label]) => h("option", { value, text: label })));
      control.value = spec.value;
    } else {
      control = h("input", { class: "input", value: spec.value, placeholder: spec.placeholder || (key.includes("price") ? "未配置" : "") });
    }
    inputs[key] = control;
    formGrid.appendChild(h("div", { class: "field" }, h("label", { text: spec.label }), control));
  }
  priceBody.appendChild(formGrid);
  priceBody.appendChild(
    h(
      "div",
      { class: "flex" },
      button("保存价格版本", async () => {
        const payload = Object.fromEntries(Object.entries(inputs).map(([key, el]) => [key, el.value.trim() === "" ? null : el.value.trim()]));
        payload.min_input_tokens = Number(payload.min_input_tokens) || 0;
        try {
          await api.post("prices/save", payload);
          toast("已保存价格版本");
          await ensureCosts(true);
          renderCostsView();
        } catch (error) {
          handleError(error, true);
        }
      }, { kind: "primary", small: false }),
    ),
  );
  if (state.prices.length) {
    const table = h("table", { class: "data" });
    table.appendChild(
      h("thead", {}, h("tr", {}, ["Provider","模型","档位","起算","币种","计费","输入","缓存","输出","状态"].map((label) => h("th", { text: label })))),
    );
    const tbody = h("tbody");
    for (const price of state.prices) {
      tbody.appendChild(
        h("tr", {},
          h("td", { class: "mono", text: price.provider_id }),
          h("td", { class: "mono", text: price.model }),
          h("td", { text: price.tier_label || "—" }),
          h("td", { class: "num", text: fmtCompact(price.min_input_tokens) }),
          h("td", { text: price.currency }),
          h("td", { text: price.billing_mode }),
          h("td", { class: "num", text: price.input_price ?? "—" }),
          h("td", { class: "num", text: price.cached_input_price ?? "—" }),
          h("td", { class: "num", text: price.output_price ?? "—" }),
          h("td", {}, price.is_active ? badge("生效中", "green") : badge("历史", "line")),
        ),
      );
    }
    table.appendChild(tbody);
    priceBody.appendChild(h("div", { class: "table-wrap", style: { maxHeight: "300px" } }, table));
  }
  priceCard.appendChild(priceBody);

  // Settings + health.
  const settingsCard = h("div", { class: "card" });
  settingsCard.appendChild(h("div", { class: "card-head" }, h("h2", { text: "观察台设置" })));
  const settingsBody = h("div", { class: "card-body stack" });
  const settingFields = {
    capture_enabled: ["采集主聊天", "bool"],
    record_incoming_messages: ["补采收到消息", "bool"],
    record_outgoing_messages: ["记录发送", "bool"],
    retention_chat_days: ["聊天保留（天）", "int"],
    retention_reasoning_days: ["思考保留（天）", "int"],
    retention_snapshot_days: ["快照保留（天）", "int"],
    retention_usage_days: ["用量保留（天）", "int"],
    data_budget_gb: ["正文预算（GB）", "float"],
    refresh_seconds: ["自动刷新（秒）", "int"],
    max_message_chars: ["单条字数上限", "int"],
  };
  const settingInputs = {};
  const checks = [];
  const grid = h("div", { class: "form-grid" });
  for (const [key, [label, type]] of Object.entries(settingFields)) {
    if (type === "bool") {
      const input = h("input", { type: "checkbox" });
      input.checked = Boolean(settings[key]);
      settingInputs[key] = input;
      checks.push(h("label", { class: "checkline" }, input, h("span", { text: label })));
    } else {
      const input = h("input", { class: "input", type: "number", value: settings[key] ?? "" });
      settingInputs[key] = input;
      grid.appendChild(h("div", { class: "field" }, h("label", { text: label }), input));
    }
  }
  settingsBody.appendChild(h("div", { class: "pill-row" }, checks));
  settingsBody.appendChild(grid);
  settingsBody.appendChild(
    h("div", { class: "flex" },
      button("保存设置", async () => {
        const payload = {};
        for (const [key, [label, type]] of Object.entries(settingFields)) {
          const input = settingInputs[key];
          if (type === "bool") payload[key] = Boolean(input.checked);
          else if (type === "int") payload[key] = Number(input.value);
          else payload[key] = Number(input.value);
        }
        try {
          const saved = await api.post("settings/save", payload);
          state.settings = saved.settings;
          state.refreshSeconds = saved.settings.refresh_seconds ?? state.refreshSeconds;
          startPolling();
          toast("设置已保存");
          await ensureCosts(true);
          renderCostsView();
        } catch (error) {
          handleError(error, true);
        }
      }, { kind: "primary", small: false }),
    ),
  );
  settingsBody.appendChild(
    h("div", { class: "tiny muted", text: "关闭采集后停止新增记录，已有数据仍可查看。" }),
  );
  settingsCard.appendChild(settingsBody);

  const healthCard = h("div", { class: "card" });
  healthCard.appendChild(h("div", { class: "card-head" }, h("h2", { text: "采集健康" })));
  const healthBody = h("div", { class: "card-body stack" });
  const health = state.health || {};
  const capture = health.capture || {};
  const counts = health.counts || {};
  healthBody.appendChild(
    h("div", { class: "kv-grid" },
      kv("采集包装", capture.wrapper_in_place ? "生效" : "未生效"),
      kv("压缩包装", capture.compression_wrapper_in_place ? "生效" : "未生效"),
      kv("队列深度", fmtNumber(health.collector?.queue_depth)),
      kv("最近写入", fmtTime(health.storage?.last_write_at, true)),
      kv("请求 / 回复轮次", `${fmtNumber(counts.requests)} / ${fmtNumber(counts.traces)}`),
      kv("未知用量", fmtNumber(counts.unknown_usage)),
      kv("思考可用", fmtNumber(counts.reasoning_available)),
      kv("快照已过期", fmtNumber(counts.expired_snapshots)),
    ),
  );
  if (health.recent_gaps?.length) {
    const gaps = h("div", { class: "note danger" });
    gaps.appendChild(h("div", { text: "采集缺口" }));
    for (const gap of health.recent_gaps.slice(0, 5)) {
      gaps.appendChild(h("div", { class: "tiny", text: `${fmtTime(gap.ts)} · ${gap.kind} · ${gap.detail || ""}` }));
    }
    healthBody.appendChild(gaps);
  }
  const exportRow = h("div", { class: "export-grid" });
  for (const [kind, label] of [["requests", "请求"], ["traces", "回复轮次"], ["messages", "消息"], ["costs", "费用"]]) {
    exportRow.appendChild(
      h("div", { class: "export-row" },
        h("span", { text: label }),
        h("div", { class: "pill-row" },
          button("JSON", () => exportData(kind, "json")),
          button("CSV", () => exportData(kind, "csv")))),
    );
  }
  healthBody.appendChild(exportRow);
  healthBody.appendChild(
    h("div", { class: "tiny muted", text: "仅当前筛选范围，不含密钥与模型配置。" }),
  );
  healthCard.appendChild(healthBody);

  root.appendChild(
    h("div", { class: "split-2" },
      h("div", { class: "stack" }, chartCard, costTableCard, priceCard),
      h("div", { class: "stack" }, settingsCard, healthCard),
    ),
  );
  window.scrollTo(0, previousScroll);
}

async function exportData(kind, format) {
  const { from, to } = rangeParams();
  try {
    await api.download("export", { kind, format, from, to }, `observatory_${kind}.${format}`);
    toast("已开始导出");
  } catch (error) {
    handleError(error, true);
  }
}

// ------------------------------------------------------------------- drawer
async function openRequest(requestId) {
  state.drawer.open = true;
  state.drawer.requestId = requestId;
  state.drawer.detail = null;
  state.drawer.content = {};
  state.drawer.tab = "input";
  $("drawer").classList.remove("hidden");
  $("drawer").setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  renderDrawerLoading();
  try {
    state.drawer.detail = await api.get(`requests/${encodeURIComponent(requestId)}`);
    renderDrawer();
  } catch (error) {
    handleError(error, true);
    closeDrawer();
  }
}

async function openTrace(traceId) {
  try {
    const data = await api.get(`traces/${encodeURIComponent(traceId)}`);
    const first = data.requests?.[0];
    if (first) {
      await openRequest(first.request_id);
    } else {
      toast("该回复轮次没有直接请求记录");
    }
  } catch (error) {
    handleError(error, true);
  }
}

function closeDrawer() {
  state.drawer.open = false;
  $("drawer").classList.add("hidden");
  $("drawer").setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
}

function renderDrawerLoading() {
  clear($("drawer-head")).appendChild(h("div", { class: "muted", text: "正在读取请求…" }));
  clear($("drawer-tabs"));
  clear($("drawer-body"));
}

function renderDrawer() {
  const detail = state.drawer.detail;
  if (!detail) return;
  const request = detail.request;
  const result = detail.result || {};
  const status = statusBadge(request.status);
  clear($("drawer-head")).appendChild(
    h(
      "div",
      { class: "line" },
      h(
        "div",
        {},
        h("h2", { text: "请求详情" }),
        h(
          "div",
          { class: "sub" },
          badge(status.label, status.kind),
          request.is_fallback ? badge("回退", "amber") : null,
          request.streaming ? badge("流式", "accent") : badge("非流式", "line"),
          badge(request.provider_model || "未知模型", "line"),
          h("span", { class: "mono", text: request.request_id }),
        ),
      ),
      h(
        "div",
        { class: "flex" },
        button("JSON", () => showJson()),
        h("button", { class: "icon-btn", onclick: closeDrawer, title: "关闭", "aria-label": "关闭" }, icon("close", 16)),
      ),
    ),
  );

  const tabs = [
    ["input", "输入"],
    ["reply", "回复"],
    ["reasoning", "思考"],
    ["tools", "工具"],
    ["cost", "费用"],
  ];
  clear($("drawer-tabs"));
  for (const [key, label] of tabs) {
    $("drawer-tabs").appendChild(
      h("button", {
        class: `tab ${state.drawer.tab === key ? "active" : ""}`.trim(),
        text: label,
        onclick: () => {
          state.drawer.tab = key;
          renderDrawer();
        },
      }),
    );
  }

  const body = clear($("drawer-body"));
  if (state.drawer.tab === "input") renderInputTab(body, detail);
  else if (state.drawer.tab === "reply") renderReplyTab(body, detail, result);
  else if (state.drawer.tab === "reasoning") renderReasoningTab(body, detail, result);
  else if (state.drawer.tab === "tools") renderToolsTab(body, detail);
  else renderCostTab(body, detail);
}

async function showJson() {
  try {
    const requestId = state.drawer.requestId;
    const data = await api.get(`requests/${encodeURIComponent(requestId)}/content`, { kind: "json" });
    if (!data.snapshot) {
      toast(data.note || "快照不可用", true);
      return;
    }
    const body = clear($("drawer-body"));
    body.appendChild(
      h("div", { class: "section-title" }, h("span", { text: "结构化快照 · 已脱敏" })),
    );
    const text = JSON.stringify(data.snapshot, null, 2);
    body.appendChild(textBlock(text.slice(0, 400000), { mono: true }));
  } catch (error) {
    handleError(error, true);
  }
}

async function ensureContent(kind) {
  if (state.drawer.content[kind]) return state.drawer.content[kind];
  const requestId = state.drawer.requestId;
  const data = await api.get(`requests/${encodeURIComponent(requestId)}/content`, { kind });
  state.drawer.content[kind] = data;
  return data;
}

async function loadFullText(kind) {
  const requestId = state.drawer.requestId;
  let cursor = 0;
  let text = "";
  for (let i = 0; i < 20; i += 1) {
    const data = await api.get(`requests/${encodeURIComponent(requestId)}/content`, { kind, cursor, limit: 60000 });
    if (data.state !== "available") return data;
    text += data.text || "";
    if (data.next_cursor === null || data.next_cursor === undefined) {
      return { ...data, text };
    }
    cursor = data.next_cursor;
  }
  return { state: "available", text, truncated: true };
}

async function renderInputTab(body, detail) {
  const request = detail.request;
  const blobState = { available: "可用", expired: "已过期", not_saved: "未保存", missing: "未保存", unavailable: "不可用" };
  if (detail.capture_note) {
    body.appendChild(h("div", { class: "note info", text: detail.capture_note }));
  }
  body.appendChild(
    h(
      "div",
      { class: "kv-grid", style: { marginTop: "10px" } },
      kv("消息条数", fmtNumber(request.input_msg_count)),
      kv("估算 token", fmtTokens(request.input_est_tokens)),
      kv("快照", blobState[request.input_blob_state] || request.input_blob_state || "—"),
      kv("系统提示字符", fmtNumber(request.system_prompt_chars)),
    ),
  );
  const sections = request.input_sections || [];
  if (sections.length) {
    body.appendChild(h("div", { class: "section-title" }, h("span", { text: "输入分组" })));
    body.appendChild(
      h("div", { class: "pill-row" }, sections.map((section) =>
        badge(`${section.label} · ${fmtCompact(section.tokens_estimate)}`, "line"),
      )),
    );
  }
  body.appendChild(h("div", { class: "section-title" }, h("span", { text: "发送的内容" })));
  const container = h("div");
  body.appendChild(container);
  let data;
  try {
    data = await ensureContent("input");
  } catch (error) {
    handleError(error, true);
    return;
  }
  if (data.state !== "available") {
    container.appendChild(h("div", { class: "note danger", text: data.note || "快照已过期或未保存" }));
    return;
  }
  const renderPage = (messages) => {
    for (const message of messages) {
      const group = sections.find((section) => (section.message_indices || []).includes(message.index));
      const summary = h(
        "summary",
        {},
        badge(message.role, message.role === "system" ? "accent" : message.group === "tool_result" ? "amber" : message.group === "think" ? "violet" : "line"),
        h("span", { text: GROUP_LABELS[message.group] || message.group || "未分类" }),
        h("span", { class: "tiny muted", text: `${fmtCompact(message.est_tokens)} token` }),
        message.no_save ? badge("临时", "violet") : null,
        message.dropped_parts ? badge(`省略 ${message.dropped_parts} 块`, "amber") : null,
      );
      const row = h("details", { class: "message-row" });
      row.appendChild(summary);
      const inner = h("div", { class: "body" });
      for (const part of message.parts || []) {
        if (part.type === "text") {
          inner.appendChild(textBlock(part.text, { klass: message.role === "system" ? "prompt-block" : "" }));
        } else if (part.type === "think") {
          inner.appendChild(textBlock(part.think, { klass: "think-block" }));
          if (part.encrypted_present) inner.appendChild(h("div", { class: "tiny muted", text: "该历史思考带 opaque signature，内容未解码、未保存。" }));
        } else if (part.type === "image_url") {
          inner.appendChild(h("div", { class: "attachment", text: `图片${part.data_uri ? "（本地 data URI，未保存 base64）" : ""}${part.url ? ` · ${part.url}` : ""}` }));
        } else if (part.type === "audio_url") {
          inner.appendChild(h("div", { class: "attachment", text: "音频（未保存 base64）" }));
        } else if (part.type === "checkpoint") {
          inner.appendChild(h("div", { class: "tiny muted", text: `内部检查点 ${part.id || ""}` }));
        } else {
          inner.appendChild(h("div", { class: "tiny muted", text: `未识别内容块：${part.type}` }));
        }
      }
      for (const call of message.tool_calls || []) {
        inner.appendChild(h("div", { class: "note", text: `工具调用 ${call.name || "?"} ${call.arguments || ""}` }));
      }
      row.appendChild(inner);
      container.appendChild(row);
    }
  };
  renderPage(data.messages || []);
  if (data.tool_schema?.length) {
    container.appendChild(
      collapsible(
        `工具 schema · ${data.tool_schema.length} 个 · ${fmtCompact(data.tool_schema_est_tokens || 0)} token`,
        h("div", { class: "stack" }, data.tool_schema.map((tool) =>
          h("div", { class: "note", text: `${tool.name}${tool.parameters?.length ? ` · 参数：${tool.parameters.join(", ")}` : ""}` }),
        )),
      ),
    );
  }
  if (data.next_cursor !== null && data.next_cursor !== undefined) {
    container.appendChild(
      h("div", { style: { textAlign: "center", marginTop: "10px" } }, button("加载更多消息", async () => {
        const next = await api.get(`requests/${encodeURIComponent(state.drawer.requestId)}/content`, {
          kind: "input",
          cursor: data.next_cursor,
        });
        renderPage(next.messages || []);
        if (next.next_cursor === null || next.next_cursor === undefined) {
          data.next_cursor = null;
        } else {
          data.next_cursor = next.next_cursor;
        }
      })),
    );
  }
}

async function renderReplyTab(body, detail, result) {
  const status = statusBadge(result.reply_state === "expired" ? "expired" : "available");
  body.appendChild(h("div", { class: "section-title" }, h("span", { text: "模型回复" }), badge(status.label, status.kind)));
  const holder = h("div");
  body.appendChild(holder);
  holder.appendChild(emptyState("读取中", "加载回复正文…"));
  try {
    const data = await loadFullText("reply");
    clear(holder);
    if (data.state !== "available") {
      holder.appendChild(h("div", { class: "note danger", text: data.note || "回复正文不可用" }));
      return;
    }
    holder.appendChild(textBlock(data.text || "（空）"));
    if (data.truncated) holder.appendChild(h("div", { class: "tiny muted", text: "仅显示前 1.2M 字符，完整内容可导出 JSON。" }));
  } catch (error) {
    clear(holder);
    holder.appendChild(h("div", { class: "note danger", text: error.message || "加载失败" }));
  }
  body.appendChild(h("div", { class: "section-title" }, h("span", { text: "关联" })));
  body.appendChild(
    h("div", { class: "kv-grid" },
      kv("回复轮次", detail.request.trace_id),
      kv("请求状态", detail.request.status),
      kv("延迟", fmtDuration(result.latency_ms)),
      kv("首 token", fmtDuration(result.ttft_ms)),
    ),
  );
  const traceButton = button("回复轮次详情", () => openTrace(detail.request.trace_id));
  body.appendChild(h("div", { class: "flex", style: { marginTop: "10px" } }, traceButton));
}

async function renderReasoningTab(body, detail, result) {
  const statusKey = result.reasoning_status || "not_provided";
  const status = statusBadge(result.reasoning_state === "expired" ? "expired" : statusKey);
  body.appendChild(
    h("div", { class: "section-title" },
      h("span", { text: "公开思考" }),
      badge(status.label, status.kind),
    ),
  );
  if (statusKey === "not_provided") {
    body.appendChild(
      emptyState(
        "未提供思考",
        result.signature_present ? "上游只返回了不可读的签名。" : "上游没有返回可读的思考文本。",
      ),
    );
    return;
  }
  const holder = h("div");
  body.appendChild(holder);
  holder.appendChild(emptyState("读取中", "加载公开思考…"));
  try {
    const data = await loadFullText("reasoning");
    clear(holder);
    if (data.state !== "available") {
      holder.appendChild(h("div", { class: "note danger", text: data.note || "思考正文不可用" }));
      return;
    }
    holder.appendChild(textBlock(data.text || "（空）", { klass: "think-block" }));
    holder.appendChild(h("div", { class: "tiny muted", text: `${result.reasoning_source || "unknown"} · ${data.total_chars || 0} 字符` }));
  } catch (error) {
    clear(holder);
    holder.appendChild(h("div", { class: "note danger", text: error.message || "加载失败" }));
  }
}

async function renderToolsTab(body, detail) {
  const holder = h("div");
  body.appendChild(holder);
  holder.appendChild(emptyState("读取中", "加载工具链路…"));
  try {
    const data = await ensureContent("tools");
    clear(holder);
    if (!data.steps?.length) {
      holder.appendChild(emptyState("没有工具调用", "这一轮请求没有使用工具。"));
      return;
    }
    for (const step of data.steps) {
      const calls = step.tool_calls || [];
      const results = step.tool_results || [];
      const card = h("div", { class: "message-row" });
      card.appendChild(
        h("div", { class: "body" },
          h("div", { class: "flex wrap" },
            badge(`步骤 ${step.step_no} / 尝试 ${step.attempt_no}`, "line"),
            badge(step.provider_model || "—", "accent"),
            statusBadge(step.status) ? badge(statusBadge(step.status).label, statusBadge(step.status).kind) : null,
          ),
          calls.length
            ? h("div", { class: "stack" }, calls.map((call) =>
                h("div", { class: "note" },
                  h("div", { text: `调用 ${call.name || "?"}（id ${call.id || "—"}）` }),
                  h("div", { class: "mono tiny", text: call.arguments || "" }),
                ),
              ))
            : h("div", { class: "tiny muted", text: "本步没有工具调用" }),
          results.length
            ? h("div", { class: "stack" }, results.map((resultItem) =>
                h("div", { class: "text-block mono", text: resultItem.text || "（工具结果为空）" }),
              ))
            : h("div", { class: "tiny muted", text: "没有后续请求输入可证明工具结果。" }),
        ),
      );
      holder.appendChild(card);
    }
  } catch (error) {
    clear(holder);
    holder.appendChild(h("div", { class: "note danger", text: error.message || "加载失败" }));
  }
}

function renderCostTab(body, detail) {
  const request = detail.request;
  const result = detail.result || {};
  const cost = detail.cost || {};
  const context = request.context || {};
  body.appendChild(
    h(
      "div",
      { class: "kv-grid" },
      kv("输入（非缓存）", fmtTokens(result.usage?.input_other)),
      kv("缓存输入", fmtTokens(result.usage?.input_cached)),
      kv("输出", fmtTokens(result.usage?.output)),
      kv("用量来源", result.usage_source || "unknown"),
      kv("本次估算", cost.amount ? fmtMoney(cost.amount, cost.currency) : statusBadge(cost.status).label),
      kv("计费状态", statusBadge(cost.status).label),
    ),
  );
  if (result.usage_source !== "api_reported") {
    body.appendChild(h("div", { class: "note", text: result.usage_missing_reason || "模型未返回完整用量；未知用量不按 0 计费。" }));
  } else if (cost.reason) {
    body.appendChild(h("div", { class: "note info", text: cost.reason }));
  }
  body.appendChild(h("div", { class: "section-title" }, h("span", { text: "本轮上下文" })));
  const total = (result.usage?.input_other || 0) + (result.usage?.input_cached || 0) + (result.usage?.output || 0);
  body.appendChild(
    h("div", { class: "kv-grid" },
      kv("本次请求 token", fmtNumber(total)),
      kv("管理预算", context.budget_tokens ? fmtTokens(context.budget_tokens) : "未设置"),
      kv("压缩触发点", context.trigger_tokens ? `约 ${fmtTokens(context.trigger_tokens)}` : "未启用"),
      kv("压缩策略", context.strategy || "—"),
      kv("触发阈值", context.threshold ?? "—"),
      kv("保留最近比例", context.keep_recent_ratio ?? "—"),
    ),
  );
  if (detail.compressions?.length) {
    body.appendChild(h("div", { class: "section-title" }, h("span", { text: "本轮关联的压缩事件" })));
    for (const event of detail.compressions) {
      body.appendChild(
        h("div", { class: "note" },
          h("div", { text: `${fmtTime(event.created_at)} · ${event.strategy || "—"} · ${fmtCompact(event.before_tokens)} → ${fmtCompact(event.after_tokens)} token` }),
          h("div", { class: "tiny muted", text: `触发预算 ${fmtCompact(event.trigger_budget)} · 消息 ${event.before_messages} → ${event.after_messages} · 摘要${event.summary_blob_id ? "已保存" : "未采集"} · ${event.token_source || ""}` }),
        ),
      );
    }
  }
  body.appendChild(
    h("div", { class: "note info", text: "费用按已配置单价估算，不是供应商账单；未知单价与用量不会显示为 0。" }),
  );
}

// ---------------------------------------------------------------------- init
boot().catch((error) => {
  console.error(error);
  toast(error.message || "初始化失败", true);
});
