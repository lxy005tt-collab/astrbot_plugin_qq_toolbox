(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let day = "";
  let busy = false;
  const bridge = window.AstrBotPluginPage;

  function unwrap(response) {
    const body = response?.data?.status ? response.data : response;
    if (body?.status !== "ok") throw new Error(body?.message || "读取失败，请刷新后重试。");
    return body.data;
  }

  function notice(text, error = false) {
    $("notice").textContent = text;
    $("notice").classList.toggle("error", error);
  }

  function setBusy(value) {
    busy = value;
    document.querySelectorAll("button").forEach(button => { button.disabled = value || !day; });
    $("refresh").disabled = value;
  }

  async function load() {
    const data = unwrap(await bridge.apiGet("page/quota", {}));
    day = data.day;
    $("summary").textContent = `北京时间 ${day} · 每位群友每天 ${data.limit} 次 · ${data.enabled ? "限额已开启" : "限额已关闭"}`;
    $("policy").textContent = `跨群共用额度，每天 00:00 自动恢复。${data.admin_exempt ? "AstrBot 管理员免限额。" : ""}失败和取消不计次数。`;
    const rows = data.entries || [];
    $("usage").replaceChildren();
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const value of [row.sender_id, row.used, row.pending, row.remaining]) {
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.append(td);
      }
      const td = document.createElement("td");
      const button = document.createElement("button");
      button.textContent = "重置今日";
      button.type = "button";
      button.addEventListener("click", () => reset("user", row.sender_id));
      td.append(button);
      tr.append(td);
      $("usage").append(tr);
    }
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = "今天还没有计入额度的绘图任务。";
      tr.append(td);
      $("usage").append(tr);
    }
  }

  async function run(action) {
    if (busy) return;
    setBusy(true);
    try { await action(); }
    catch (error) { notice(error.message || String(error), true); }
    finally { setBusy(false); }
  }

  async function reset(scope, senderId) {
    await run(async () => {
      const result = unwrap(await bridge.apiPost("page/quota/reset", {scope, sender_id: senderId, day}));
      notice(`已清除 ${result.reset_count} 次今日已完成记录，进行中的任务保留。`);
      await load();
    });
  }

  $("refresh").addEventListener("click", () => run(load));
  $("reset-all").addEventListener("click", () => reset("all"));
  $("reset-user").addEventListener("submit", event => {
    event.preventDefault();
    reset("user", $("qq").value.trim());
  });
  run(async () => {
    if (!bridge) throw new Error("请从 AstrBot 管理面板中的插件页面打开。");
    await bridge.ready();
    await load();
  });
})();
