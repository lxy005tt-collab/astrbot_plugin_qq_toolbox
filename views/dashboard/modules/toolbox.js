import { h, clear, badge, emptyState, button, statCard } from "./dom.js";

const VIEWS = new Set(["toolbox", "settings", "space", "quota"]);
const NOTES = {
  reply: "复读、话题插话、回复后跟进、私聊等待与人设保护。",
  input_filter: "消息进入模型前先过滤。",
  observatory: "主聊天记录、请求、用量与费用。",
  image: "生成与编辑图片，按需检索，管理群友额度。",
  output_filter: "替换词、符号屏蔽与输出保护。",
  space: "写日常、每日日记、定点发布、查看留言。",
  voice: "按概率把回复转成语音。",
  life: "遵循既定经历与课表，补充连贯的虚拟日常。",
};
const HELP = {
  reply: "智能判断和回复后跟进会额外消耗 token；主动接话仍受 AstrBot 的主动回复概率影响。",
  input_filter: "命中的输入会在交给模型前被拦截，词表沿用 AstrBot 后台的内容安全设置。",
  observatory: "记录聊天、模型请求与耗时；排查未回复时，可先打开观察台查看记录。",
  image: "先选择生图模型，再用「/画图 + 描述」生成图片；生图和资料检索会消耗接口额度。",
  output_filter: "替换只处理准备发送的文字。符号屏蔽可能误拦 Markdown、代码和颜文字，按需开启。",
  space: "随机发布、定点任务和每日日记分别启用；发布时间均按北京时间，配图可选。",
  voice: "语音合成使用后台配置的语音；可分别设置群聊和私聊的语音概率。",
  life: "按给定课表与作息提供生活背景，允许补充连贯的日常经历；已有设定和经历优先。",
};
const FIELD_HELP = {
  "reply.group_memory.enabled": "仅向指定私聊提供指定群的同人格记忆；需 LivingMemory，会增加检索耗时与输入 token。",
  "reply.follow_up.enabled": "只回顾刚回复的人的新消息；没有新消息就不调用判断模型。",
  "reply.persona_guard.enabled": "会关闭群聊流式与思考展示，实际出现泄漏时再按需开启。",
  "reply.persona_guard.filter_leaks": "可能误拦正常的 Markdown、代码或颜文字。",
  "output_filter.block_asterisk": "正文出现 * 就整条不发，加粗和带星号的颜文字也会被拦截。",
  "output_filter.block_markdown": "按字符拦截整条回复，正常标点、Markdown 和颜文字也可能命中。",
  "output_filter.word_replacements": "每行一条：原词 = 替换词。等号右侧留空表示删除。",
  "space.diary.enabled": "开启后到点会直接发布到空间，写作会消耗模型 token。",
};
const LABELS = { waiting: "等待执行", disabled: "已关闭", running: "执行中", completed: "已完成", expired: "时间已过", missed: "已跳过离线时段", success: "成功", failed: "失败", error: "异常", interrupted: "已中断", ready: "已连接", pending: "连接中", unavailable: "暂不可用", writing: "正在写作", draft: "草稿，尚未发布", publishing: "正在发布", published: "已发布到空间", unknown: "结果未确认，请先核对空间" };
const clone = value => JSON.parse(JSON.stringify(value));
const at = (obj, path) => path.reduce((value, key) => value?.[key], obj);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const textTime = value => /^\d{10}$/.test(String(value)) ? new Date(Number(value) * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : String(value || "暂无记录");
const shortWhen = value => {
  const match = String(value || "").match(/^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : String(value || "");
};

function readHintPreference() {
  try { return localStorage.getItem("qq-toolbox-hints") === "1"; } catch { return false; }
}

export function createToolboxUI({ api, toast, navigate }) {
  let snapshot, draft, status, quota, selected = "reply", saving = false, active = "toolbox";
  let spaceKind = "posts", spaceOffset = 0, spaceData = null, spaceLoading = false, spaceError = "";
  let showHints = readHintPreference();
  let noticeOpen = false;
  let diaryBusy = false;
  const changes = new Map();
  let loading;
  let providers = { chat: [], tts: [] };
  const invalidNumbers = new Map();

  async function loadConfig(force = false) {
    if (snapshot && !force) return;
    if (loading) return loading;
    loading = Promise.all([api.get("suite/config"), api.get("suite/providers")]).then(([data, catalog]) => {
      snapshot = data;
      providers = catalog;
      draft = clone(data.config);
      changes.clear();
    }).finally(() => { loading = null; });
    await loading;
  }

  function change(path, value, extra = {}) {
    let target = draft;
    for (const key of path.slice(0, -1)) target = target[key] ??= {};
    target[path.at(-1)] = clone(value);
    const key = JSON.stringify(path);
    if (same(value, at(snapshot.config, path)) && !extra.clear_secret) changes.delete(key);
    else changes.set(key, { path, value: clone(value), ...extra });
    updateSavebar();
  }

  function updateSavebar() {
    document.querySelectorAll("[data-save-config]").forEach(el => {
      el.disabled = saving || !changes.size;
      el.textContent = saving ? "正在应用…" : "保存并应用";
    });
    document.querySelectorAll("[data-draft-count]").forEach(el => {
      el.textContent = changes.size ? `${changes.size} 项未保存` : "已保存";
      el.classList.toggle("has-changes", !!changes.size);
      el.closest(".tb-savebar")?.classList.toggle("is-dirty", !!changes.size);
    });
  }

  function savebar() {
    return h("div", { class: "tb-savebar" },
      h("span", { class: "tb-count", "data-draft-count": "" }),
      h("div", { class: "tb-actions" }, button("放弃更改", () => {
        draft = clone(snapshot.config); changes.clear(); invalidNumbers.clear(); paint();
      }), h("button", { class: "btn primary", "data-save-config": "", onclick: save }, "保存并应用")));
  }

  async function save() {
    if (saving || !changes.size) return;
    if (invalidNumbers.size) { toast(`请填写有效数字：${invalidNumbers.values().next().value}`, true); return; }
    const section = document.getElementById(`view-${active}`);
    const invalid = Array.from(section.querySelectorAll("input, textarea, select")).find(el => !el.checkValidity());
    if (invalid) { invalid.reportValidity(); return; }
    saving = true; updateSavebar();
    try {
      await api.post("suite/config/save", { revision: snapshot.revision, changes: Array.from(changes.values()) });
      // Keep drafts until the save is acknowledged; polling never clears them.
      changes.clear(); snapshot = null;
      await loadConfig();
      status = await api.get("suite/status");
      toast("已保存并应用");
      paint();
    } catch (error) { toast(error.message || "保存失败，修改仍保留在页面中。", true); }
    finally { saving = false; updateSavebar(); }
  }

  function panel(title, detail, ...children) {
    return h("section", { class: "tb-panel" }, h("div", { class: "tb-panel-head" },
      h("h2", { text: title }), detail ? h("p", { text: detail }) : null), ...children);
  }

  function textLink(label, onclick, quiet = false) {
    return h("button", { class: `tb-link ${quiet ? "quiet" : ""}`.trim(), text: label, onclick });
  }

  function hintsToggle() {
    return h("div", { class: "tb-hint-toggle" },
      h("button", {
        class: "btn ghost small tb-hints-btn",
        text: showHints ? "收起详细说明" : "展开详细说明",
        "aria-pressed": String(showHints),
        title: showHints ? "隐藏字段说明" : "显示每个字段的说明",
        onclick: () => {
          showHints = !showHints;
          try { localStorage.setItem("qq-toolbox-hints", showHints ? "1" : "0"); } catch { /* preview */ }
          paint();
        },
      }));
  }

  function fields(nodes, path, values) {
    return h("div", { class: `tb-fields ${showHints ? "show-hints" : ""}`.trim() },
      Object.entries(nodes).filter(([, spec]) => !spec.invisible).map(([key, spec]) => field(spec, [...path, key], values?.[key] ?? spec.default)));
  }

  function field(spec, path, value, setter = value => change(path, value)) {
    const label = spec.description || path.at(-1);
    if (spec.toolbox_model_kind) return modelField(spec, path);
    if (spec.toolbox_provider_kind) return providerField(spec, path, value, setter, spec.toolbox_provider_kind);
    if (spec.type === "object") {
      return h("details", { class: "tb-field-group", open: true }, h("summary", { text: label }), fields(spec.items || {}, path, value || {}));
    }
    if (spec.type === "template_list") return taskEditor(spec, value || [], setter);
    const id = "setting-" + path.join("-");
    const wrap = h("div", { class: `tb-field ${spec.type === "bool" ? "tb-switch-row" : ""} ${["text", "list"].includes(spec.type) ? "tb-wide" : ""}` });
    const essential = FIELD_HELP[path.join(".")] || (spec.type === "list" ? "每行填写一项。" : "");
    const hint = essential || spec.hint;
    const caption = h("div", { class: "tb-caption" }, h("label", { for: id, text: label, title: spec.hint || "" }), hint ? h("p", { id: `${id}-hint`, class: essential ? "tb-essential" : "tb-hint", text: hint }) : null);
    let input;
    if (spec.type === "bool") {
      input = h("input", { id, type: "checkbox", class: "tb-switch", role: "switch", checked: !!value, "aria-describedby": hint ? `${id}-hint` : null, onchange: event => setter(event.target.checked) });
      wrap.append(caption, input); return wrap;
    }
    if (spec.options) {
      input = h("select", { id, onchange: event => setter(event.target.value) }, spec.options.map(option => h("option", { value: option, text: option })));
    } else if (["text", "list"].includes(spec.type)) {
      input = h("textarea", { id, rows: spec.type === "list" ? 5 : 4, oninput: event => setter(spec.type === "list" ? event.target.value.split("\n").filter(line => line.trim()) : event.target.value) });
    } else {
      const numeric = ["int", "float"].includes(spec.type);
      input = h("input", { id, type: spec.secret ? "password" : numeric ? "number" : "text", autocomplete: spec.secret ? "new-password" : "off",
        step: numeric ? spec.slider?.step || (spec.type === "int" ? 1 : "any") : null, min: spec.slider?.min, max: spec.slider?.max,
        oninput: event => {
          if (numeric && (event.target.value === "" || !event.target.validity.valid)) {
            invalidNumbers.set(id, label); return;
          }
          invalidNumbers.delete(id);
          setter(numeric ? Number(event.target.value) : event.target.value);
        } });
      if (numeric) input.required = true;
    }
    input.value = spec.type === "list" ? (value || []).join("\n") : value ?? "";
    if (hint) input.setAttribute("aria-describedby", `${id}-hint`);
    wrap.append(caption, input);
    if (spec.secret) {
      const stored = snapshot.secrets_set.some(item => same(item, path));
      input.placeholder = stored ? "已配置，留空保留原值" : "尚未配置";
      const clearing = !!changes.get(JSON.stringify(path))?.clear_secret;
      input.disabled = clearing;
      wrap.append(h("label", { class: "tb-secret-clear" }, h("input", { type: "checkbox", checked: clearing, onchange: event => {
        input.disabled = event.target.checked;
        if (event.target.checked) { input.value = ""; change(path, "", { clear_secret: true }); }
        else change(path, input.value);
      } }), "清除已保存的密钥"));
    }
    return wrap;
  }

  function providerField(spec, path, value, setter, kind = "chat", compatible = false) {
    const rows = (providers[kind] || []).filter(row => !compatible || row.openai_compatible);
    const id = "setting-" + path.join("-");
    const select = h("select", { id, onchange: event => setter(event.target.value) },
      h("option", { value: "", text: kind === "tts" ? "跟随后台默认语音" : compatible ? "请选择 OpenAI 兼容后台模型" : "跟随当前会话 / 默认文字模型" }),
      rows.map(row => h("option", { value: row.id, text: row.model ? `${row.id} · ${row.model}` : row.id })));
    if (value && !rows.some(row => row.id === value)) select.append(h("option", { value, text: `${value}（当前不可用）` }));
    select.value = value || "";
    return h("div", { class: "tb-field" }, h("div", { class: "tb-caption" }, h("label", { for: id, text: spec.description || "后台模型" }),
      spec.hint ? h("p", { class: "tb-hint", text: spec.hint }) : null), select);
  }

  function modelField(spec, path) {
    const card = h("fieldset", { class: "tb-model-card tb-wide" }, h("legend", { text: spec.description }));
    const body = h("div", { class: `tb-fields ${showHints ? "show-hints" : ""}`.trim() });
    const sourceId = "setting-" + path.join("-") + "-source";
    const source = h("select", { id: sourceId }, spec.items.source.options.map(value => h("option", { value, text: value })));
    function draw() {
      clear(body);
      const current = at(draft, path) || {};
      source.value = current.source || spec.items.source.default;
      if (source.value === "自定义接口") {
        for (const key of ["base_url", "api_key", "model"]) body.append(field(spec.items[key], [...path, key], current[key] || ""));
      } else if (source.value === "后台模型") {
        body.append(providerField(spec.items.provider_id, [...path, "provider_id"], current.provider_id,
          value => change([...path, "provider_id"], value), "chat", ["image", "space_image", "research"].includes(spec.toolbox_model_kind)));
      } else body.append(h("p", { class: "tb-essential tb-wide", text: "使用「画图」里配置的生图与改图模型。" }));
      if (spec.items.timeout_seconds && source.value === "自定义接口") body.append(field(spec.items.timeout_seconds, [...path, "timeout_seconds"], current.timeout_seconds ?? 60));
    }
    source.addEventListener("change", () => { change([...path, "source"], source.value); draw(); });
    card.append(h("p", { class: "tb-hint", text: spec.hint }), h("div", { class: "tb-field tb-model-source" }, h("label", { for: sourceId, text: "模型来源" }), source,
      h("p", { class: "tb-essential", text: "可选后台模型，或为此功能单独填写 OpenAI 兼容地址、Key 和模型名。" })), body);
    draw();
    return card;
  }

  function taskEditor(spec, value, setter) {
    const tasks = clone(value);
    const template = Object.values(spec.templates)[0];
    const list = h("div", { class: "tb-task-list" });
    const update = () => setter(clone(tasks));
    function drawTasks() {
      clear(list);
      if (!tasks.length) list.append(emptyState("还没有任务", "添加一条每天重复或指定日期的发布任务。"));
      tasks.forEach((task, index) => {
        const live = status?.space?.fixed_tasks?.find(entry => task.id ? entry.id === task.id : entry.when === task.when && entry.instruction === task.instruction);
        const body = h("div", { class: "tb-fields" + (showHints ? " show-hints" : "") });
        const card = h("article", { class: "tb-task" }, h("div", { class: "tb-task-head" },
          h("strong", { text: `任务 ${index + 1}` }),
          h("span", { class: "muted tiny", text: live ? `${LABELS[live.status] || live.status} · ${live.next_run_beijing || "无下一次"}` : "保存后安排执行" }),
          button("删除", () => { tasks.splice(index, 1); update(); drawTasks(); }, { kind: "danger" })), body);
        for (const [key, itemSpec] of Object.entries(template.items)) {
          const set = v => { task[key] = v; update(); };
          body.append(key === "when" ? timeField(task.when || "每天21:00", set, index) : field(itemSpec, ["task", String(index), key], task[key] ?? itemSpec.default, set));
        }
        if (live?.last_result) card.append(h("p", { class: "tb-hint", text: `上次执行：${live.last_result}` }));
        list.append(card);
      });
    }
    drawTasks();
    return h("div", { class: "tb-wide tb-tasks" }, h("div", { class: "tb-task-toolbar" },
      h("span", { class: "tb-view-note", text: "北京时间执行。" }),
      button("添加任务", () => {
        const item = { id: globalThis.crypto?.randomUUID?.() || `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}` };
        for (const [key, spec] of Object.entries(template.items)) item[key] = clone(spec.default);
        tasks.push(item); update(); drawTasks();
      })), list);
  }

  function timeField(value, setter, index) {
    const dailyMatch = String(value).match(/^(?:每天\s*)?(\d{1,2}:\d{2})$/);
    const mode = h("select", { "aria-label": `任务 ${index + 1} 时间类型` }, h("option", { value: "daily", text: "每天" }), h("option", { value: "once", text: "指定日期一次" }));
    mode.value = dailyMatch ? "daily" : "once";
    const input = h("input", { type: dailyMatch ? "time" : "datetime-local", required: true, "aria-label": `任务 ${index + 1} 发布时间（北京时间）` });
    input.value = dailyMatch ? dailyMatch[1].padStart(5, "0") : String(value).replace(" ", "T");
    function update() { if (input.value) setter(mode.value === "daily" ? "每天" + input.value : input.value); }
    mode.addEventListener("change", () => {
      input.type = mode.value === "daily" ? "time" : "datetime-local";
      input.value = mode.value === "daily" ? "21:00" : "";
      if (!input.value) setter(""); else update();
    });
    input.addEventListener("input", update);
    return h("div", { class: "tb-field" }, h("label", { text: "发布时间" }), h("div", { class: "tb-time-input" }, mode, input));
  }

  function newUserNotice() {
    const items = [
      ["保护开关按需开启",
        "默认关闭人设保护、草稿拦截、Markdown 符号屏蔽和独立星号屏蔽。实际出现思考或人设泄漏时再开启；这些规则可能误拦 Markdown、代码和颜文字。群聊人设保护还会关闭群聊流式与思考展示。"],
      ["每个功能可单独配置模型",
        "可选择后台模型，或独立填 OpenAI 兼容 URL、Key 和模型名。聊天、生图和检索的接口能力不同，检索要求 Responses / web_search；语音合成使用后台语音。内置三视图不代替生图接口。"],
      ["主动回复会额外消耗 token",
        "艾特判断、主动判断和跟进回顾会额外调用模型。跟进默认只观察刚回复的人 10 秒，没有新消息就不回顾；普通主动接话仍受 AstrBot 的主动回复概率与冷却限制。"],
      ["话题插话不会自动点名",
        "开启话题插话后，bot 可按当前人格评论群聊话题或群体现象，不自动艾特、引用最后一个人，也不为他开启跟进。仍需通过相关性判断。"],
      ["群记忆共享需指定范围",
        "默认关闭；启用后仅向指定私聊提供指定群的同人格历史记忆，群聊不读取私聊。需要 LivingMemory，增加检索耗时与输入 token；接收者应有权查看来源群内容。"],
      ["艾特后会先收集消息",
        "默认等待 10 秒，将原始艾特及同一用户在窗口内的消息一起判断；群聊背景另限 12 条。管理员默认豁免，普通成员还受休眠、冷却和唤醒设置影响。"],
      ["空间发布与配图分开开启",
        "随机发布每轮从设定的 1～144 小时间隔内抽取，定点任务按北京时间执行。先检查登录账号及发文权限；文案、配图和检索可能分别计费。"],
      ["日记写完会直接发到空间",
        "到点参考当天参与并回复过的私聊和自己的生活写作，不读取群聊；素材从启用后积累，本机保留近 7 天。可先看草稿，注意他人隐私；离线错过不补发，同一天不重复发布。"],
      ["等图看进度，补发用原图",
        "生图完成后自动发送，任务未完成时不会重复生成；补发复用原图。普通场景可选按需检索减少等待，接口发送成功仍需以 QQ 实际显示为准。"],
      ["没回复先看观察台",
        "先查过滤、模型错误、冷却和休眠。输入过滤用后台词表，输出替换可能改动正常文字；采集记录含聊天和请求内容，分享前先检查隐私。"],
    ];
    return h("details", {
      class: "tb-new-user",
      open: noticeOpen,
      ontoggle: event => { noticeOpen = event.target.open; },
    }, h("summary", {},
      h("span", { class: "tb-new-user-title", text: "新用户必读" }),
      h("span", { class: "tb-new-user-action", text: "点击展开 / 收起注意事项" })),
    h("div", { class: "tb-new-user-body" }, items.map(([title, text]) =>
      h("section", {}, h("h3", { text: title }), h("p", { text })))));
  }

  function home(root) {
    const schedule = status?.space?.schedule;
    root.append(newUserNotice());
    root.append(h("div", { class: "tb-shortcuts", "aria-label": "常用入口" },
      button("功能设置", () => navigate("settings"), { kind: "primary", small: false }),
      button("空间与日记", () => navigate("space"), { small: false }),
      button("打开观察台", () => navigate("overview"), { small: false })));
    root.append(h("div", { class: "stats tb-stats" },
      statCard({ label: "运行状态", value: status?.enabled ? "运行中" : "已暂停", sub: `${status?.module_count || 0} / 8 个模块已加载` }),
      statCard({ label: "下次主动发布", value: schedule?.next_run_beijing ? shortWhen(schedule.next_run_beijing) : "未安排", sub: schedule?.bounds ? `间隔 ${schedule.bounds.join("–")} 小时` : "未启用随机发布" }),
      statCard({ label: "空间连接", value: LABELS[status?.space?.probe?.state] || "未启用", sub: status?.space?.probe?.qq ? `QQ ${status.space.probe.qq}` : "跟随当前登录" })));
    root.append(h("div", { class: "tb-home-head" },
      h("h2", { text: "模块" }), h("span", { class: "tb-count", text: `${status?.module_count || 0} / ${snapshot.modules.length}` })));
    root.append(h("div", { class: "tb-module-grid" }, snapshot.modules.map(module => {
      const loaded = !!status?.modules?.[module.key];
      return h("article", { class: "tb-module" },
        h("div", { class: "tb-module-head" }, h("h3", { text: module.display_name }), badge(loaded ? "已加载" : "未加载", loaded ? "green" : "line")),
        h("p", { text: NOTES[module.key] }),
        h("div", { class: "tb-module-foot" },
          h("button", { class: "btn tb-config-btn", text: "打开设置", "aria-label": `设置${module.display_name}`, onclick: () => { selected = module.key; navigate(module.key === "space" ? "space" : "settings"); } }),
          module.key === "observatory" ? textLink("打开观察台", () => navigate("overview"), true) : null,
          module.key === "image" ? textLink("查看额度", () => navigate("quota"), true) : null)));
    })));
    root.append(panel("总开关", null, fields({ enabled: snapshot.schema.enabled }, [], draft)), savebar());
  }

  function settings(root) {
    const navigation = h("div", { class: "tb-module-tabs", role: "group", "aria-label": "选择功能模块" }, snapshot.modules.map(module =>
      h("button", { class: selected === module.key ? "active" : "", "aria-pressed": String(selected === module.key), "data-module": module.key, onclick: () => { selected = module.key; paint(); } }, module.display_name)));
    const module = snapshot.modules.find(item => item.key === selected) || snapshot.modules[0];
    root.append(navigation,
      h("div", { class: "tb-settings-head" }, h("div", {}, h("h2", { text: module.display_name }), h("p", { class: "tb-essential", text: HELP[module.key] })), hintsToggle()),
      fields(snapshot.schema[module.key].items, [module.key], draft[module.key]));
    if (module.key === "image") root.append(characterReferences());
    root.append(savebar());
  }

  function characterReferences() {
    const characters = [
      { name: "奶蛙", file: "milk_frog_turnaround.jpg", example: "/奶蛙 在海边弹吉他" },
      { name: "大肥鱼", file: "fat_fish_turnaround.jpg", example: "/大肥鱼 在窗边喝茶" },
    ];
    return panel("内置角色三视图", "安装包已包含原图。参考图路径留空时自动使用；填写自定义路径可覆盖。",
      h("div", { class: "tb-character-grid" }, characters.map(character => {
        const url = new URL(`../assets/characters/${character.file}`, import.meta.url).href;
        return h("figure", { class: "tb-character" },
          h("img", { src: url, alt: `${character.name}内置三视图`, loading: "lazy", width: 1280, height: character.name === "奶蛙" ? 720 : 718 }),
          h("figcaption", {}, h("div", {}, h("strong", { text: character.name }), h("p", { class: "tb-hint", text: character.example })),
            h("a", { class: "btn small", href: url, download: `${character.name}三视图.jpg` }, "下载原图")));
      })));
  }

  function space(root) {
    const info = status?.space || {}, schedule = info.schedule || {}, spec = snapshot.schema.space.items;
    const subset = keys => fields(Object.fromEntries(keys.map(key => [key, spec[key]])), ["space"], draft.space);
    root.append(h("div", { class: "stats tb-stats" },
      statCard({ label: "下次随机发布", value: schedule.next_run_beijing ? shortWhen(schedule.next_run_beijing) : "未安排", sub: LABELS[schedule.status] || "等待启用" }),
      statCard({ label: "配图模型", value: info.image_backend?.model || "未连接", sub: "配图可选，由模型判断" }),
      statCard({ label: "最近发布", value: LABELS[schedule.last_status] || "暂无记录", sub: schedule.last_result || "随机与定点分别安排" })));
    root.append(h("div", { class: "tb-view-tools" },
      h("nav", { class: "tb-section-nav", "aria-label": "空间功能" },
        [["publish", "发布权限"], ["models", "模型"], ["random", "随机发布"], ["fixed", "定点任务"], ["diary", "每日日记"], ["feed", "动态与留言"]].map(([id, label]) =>
          h("a", { href: `#tb-space-${id}`, text: label }))), hintsToggle()));
    const addPanel = (id, title, detail, ...children) => {
      const node = panel(title, detail, ...children);
      node.id = `tb-space-${id}`;
      root.append(node);
    };
    addPanel("publish", "发布权限", "先开启空间连接和发文权限；配图按需要单独开启。", subset(["_module_enabled", "enabled", "publish_enabled", "image_generation_enabled"]));
    addPanel("models", "空间模型", "文案与配图可用不同接口，配图也可复用画图模块。", subset(["writing_model", "image_model"]));
    addPanel("random", "随机发布", "每次从 1～144 小时的设定范围抽取间隔，到点由模型决定写什么。", subset(["schedule_enabled", "interval_min_hours", "interval_max_hours", "schedule_writing_hint", "schedule_persona_id", "schedule_provider_id"]));
    addPanel("fixed", "定点任务", "按北京时间每天或指定日期执行，可填创作要求或直接发布的原文。", subset(["fixed_enabled", "fixed_tasks"]));
    addPanel("diary", "每日日记", "到点回顾当天参与的私聊与自己的生活，再写日记发到空间。草稿可提前预览，配图可选。",
      fields({ diary: spec.diary }, ["space"], draft.space),
      h("div", { id: "tb-diary-feed", class: "tb-space-feed" }));
    addPanel("feed", "动态与留言", "读取当前账号的空间动态、评论和留言板。", h("div", { class: "tb-actions" },
      button("空间动态", () => readSpace("posts", 0)), button("留言板", () => readSpace("guestbook", 0))),
      h("div", { id: "tb-space-feed", class: "tb-space-feed" }));
    root.append(savebar());
    paintFeed();
    paintDiary();
  }

  function paintDiary() {
    const root = document.getElementById("tb-diary-feed");
    if (!root) return;
    clear(root);
    const info = status?.space?.diary || {}, schedule = info.schedule || {};
    root.append(h("p", { class: "muted tiny", text: `下次日记：${schedule.next_run_beijing || "未安排"} · ${LABELS[schedule.status] || "未启用"}` }));
    if (info.collection_started_at) root.append(h("p", { class: "muted tiny", text: `私聊素材从 ${new Date(info.collection_started_at * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })} 起积累，仅记录正常回复过的私聊。` }));
    if (schedule.last_result) root.append(h("p", { class: "tb-prewrap muted tiny", text: schedule.last_result }));
    root.append(button(diaryBusy ? "正在处理…" : "写今日草稿（不发布）", () => diaryAction("draft"), { disabled: diaryBusy }));
    const entries = info.entries || [];
    if (!entries.length) root.append(emptyState("还没有日记", "可先写一篇草稿查看；定时开启后会到点自动写作并发布。"));
    entries.forEach(entry => {
      const content = entry.plan?.content || "", canPublish = content && ["draft", "failed", "interrupted"].includes(entry.state);
      const match = String(entry.result || "").match(/https:\/\/user\.qzone\.qq\.com\/[^\s]+/);
      root.append(h("article", { class: "tb-post" },
        h("div", { class: "tb-section-line" }, h("strong", { text: entry.day }),
          badge(LABELS[entry.state] || entry.state, entry.state === "published" ? "green" : "line")),
        content ? h("p", { class: "tb-prewrap", text: content }) : null,
        h("p", { class: "muted tiny", text: `参考 ${entry.message_count || 0} 条私聊片段 · ${entry.plan?.generate_image ? "发布时配图" : "纯文字"}` }),
        entry.error ? h("p", { class: "tb-error", role: "status", text: entry.error }) : null,
        canPublish ? button("将这篇日记发布到空间", () => diaryAction("publish", entry.day), { disabled: diaryBusy }) : null,
        match ? h("a", { class: "btn small", href: match[0], target: "_blank", rel: "noopener noreferrer", text: "查看空间原文" }) : null));
    });
  }

  async function diaryAction(action, day) {
    if (diaryBusy) return;
    if (changes.size) { toast("先保存设置，再使用日记功能。", true); return; }
    diaryBusy = true; paintDiary();
    try {
      const entry = await api.post(`suite/space/diary/${action}`, { day });
      status = await api.get("suite/status");
      toast(entry.state === "published" ? "日记已发布到空间" : "草稿已保存，尚未发布");
    } catch (error) {
      toast(error.message || "日记操作失败", true);
      try { status = await api.get("suite/status"); } catch { /* keep previous snapshot */ }
    } finally { diaryBusy = false; paintDiary(); }
  }

  async function readSpace(kind, offset = 0, postId = "") {
    if (spaceLoading) return;
    spaceLoading = true; spaceError = ""; paintFeed();
    try {
      spaceData = await api.post("suite/space/read", { kind, offset, count: 10, post_id: postId });
      spaceKind = kind; spaceOffset = offset;
    } catch (error) { spaceError = error.message; }
    finally { spaceLoading = false; paintFeed(); }
  }

  function commentNode(comment) {
    return h("div", { class: "tb-comment" }, h("div", { class: "tb-section-line" }, h("strong", { text: comment.name || comment.qq || "空间用户" }),
      h("span", { class: "muted tiny", text: textTime(comment.time) })), h("p", { class: "tb-prewrap", text: comment.text || "（无文字）" }),
      (comment.replies || []).map(reply => commentNode(reply)));
  }

  function paintFeed() {
    const root = document.getElementById("tb-space-feed");
    if (!root) return;
    clear(root);
    if (spaceLoading) { root.append(h("p", { class: "muted tiny", text: "正在读取…" })); return; }
    if (spaceError) { root.append(h("p", { class: "tb-error", role: "alert", text: spaceError })); return; }
    if (!spaceData) { root.append(emptyState("选择动态或留言板", "点击上方按钮读取最新内容。")); return; }
    const posts = spaceKind === "comments" ? [spaceData] : spaceData.posts;
    if (posts) {
      if (!posts.length) root.append(emptyState("暂无动态"));
      posts.forEach(post => root.append(h("article", { class: "tb-post" }, h("div", { class: "tb-section-line" }, h("span", { class: "muted tiny", text: textTime(post.time) }),
        button(`评论 ${post.comment_count || 0}`, () => readSpace("comments", 0, post.id))), h("p", { class: "tb-prewrap", text: post.text || "（无文字动态）" }),
        (post.comments || []).map(commentNode))));
      if (spaceKind === "comments" && !spaceData.comments?.length) root.append(h("p", { class: "muted tiny", text: "暂无评论。" }));
    } else {
      if (!spaceData.messages?.length) root.append(emptyState("暂无留言"));
      (spaceData.messages || []).forEach(message => root.append(commentNode(message)));
    }
    if (spaceKind !== "comments") root.append(h("div", { class: "tb-actions" }, button("上一页", () => readSpace(spaceKind, Math.max(0, spaceOffset - 10)), { disabled: !spaceOffset }),
      h("span", { class: "muted tiny", text: `${Math.floor(spaceOffset / 10) + 1} / ${Math.ceil((spaceData.total || 0) / 10) || 1} 页 · ${spaceData.total || 0} 条` }),
      button("下一页", () => readSpace(spaceKind, spaceOffset + 10), { disabled: spaceOffset + 10 >= (spaceData.total || 0) })));
  }

  function quotaView(root) {
    if (!status?.modules?.image) { root.append(emptyState("画图模块未启用", "到设置中开启后再查看额度。")); return; }
    if (!quota) { root.append(emptyState("正在读取额度")); return; }
    root.append(h("div", { class: "stats tb-stats" }, statCard({ label: "每人额度", value: String(quota.limit), sub: quota.enabled ? "跨群共用 · 每日 00:00 重置" : "未开启限额" }),
      statCard({ label: "今日使用", value: String(quota.entries?.length || 0), sub: quota.day }), statCard({ label: "管理员", value: quota.admin_exempt ? "免限额" : "参与限额", sub: "失败和取消不计入" })));
    const table = h("table", { class: "tb-table" }, h("thead", {}, h("tr", {}, ["QQ", "已完成", "进行中", "剩余", "操作"].map(text => h("th", { text })))),
      h("tbody", {}, (quota.entries || []).map(row => h("tr", {}, [row.sender_id, row.used, row.pending, row.remaining].map(text => h("td", { text })), h("td", {}, button("重置", () => resetQuota("user", row.sender_id)))))));
    const sender = h("input", { type: "text", inputmode: "numeric", pattern: "[0-9]{5,15}", placeholder: "QQ 号", "aria-label": "要重置额度的 QQ 号", required: true });
    const form = h("form", { class: "tb-actions", onsubmit: event => { event.preventDefault(); resetQuota("user", sender.value.trim()); } }, sender, h("button", { class: "btn", type: "submit", text: "重置该 QQ" }));
    root.append(panel("今日用量", null, h("div", { class: "tb-table-scroll" }, table),
      !quota.entries?.length ? emptyState("今天还没有绘图记录") : null,
      h("div", { class: "tb-section-line" }, form, button("重置全部", () => resetQuota("all"), { kind: "danger" }))));
  }

  async function resetQuota(scope, senderId) {
    if (saving) return;
    saving = true;
    try {
      const result = await api.post("page/quota/reset", { scope, sender_id: senderId, day: quota.day });
      toast(`已清除 ${result.reset_count} 次今日已完成记录`);
      quota = await api.get("page/quota"); if (active === "quota") paint();
    } catch (error) { toast(error.message, true); }
    finally { saving = false; updateSavebar(); }
  }

  function paint() {
    const root = document.getElementById(`view-${active}`);
    clear(root);
    if (active === "toolbox") home(root);
    if (active === "settings") settings(root);
    if (active === "space") space(root);
    if (active === "quota") quotaView(root);
    updateSavebar();
    document.getElementById("health-dot").className = `dot ${status?.enabled ? "ok" : "warn"}`;
    document.getElementById("health-text").textContent = `${status?.module_count || 0}/8 模块已加载`;
    document.getElementById("build-note").textContent = `v${status?.version || "—"}`;
  }

  return {
    handles: view => VIEWS.has(view),
    async render(view, background = false, reread = false) {
      active = view;
      const reloaded = reread && !changes.size;
      await loadConfig(reloaded);
      status = await api.get("suite/status");
      if (view === "quota" && status.modules?.image) quota = await api.get("page/quota");
      if (view === "space" && background) paintDiary();
      // Editing forms survive auto refresh, page switches and request failures.
      if (active !== view) return;
      const root = document.getElementById(`view-${view}`);
      if (!background || reloaded || !root.childElementCount || (view === "toolbox" && !changes.size) || view === "quota") {
        invalidNumbers.clear(); paint();
      }
    },
    get dirty() { return !!changes.size; },
  };
}
