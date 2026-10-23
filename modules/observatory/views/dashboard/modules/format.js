// Formatting helpers. All timestamps are UTC epoch seconds and are rendered in
// the user's dashboard timezone (Asia/Shanghai by default).

const TZ = "Asia/Shanghai";

const timeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const dateTimeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const fullFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function fmtTime(ts, full = false) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  return full ? `${fullFmt.format(d)}` : `${dateTimeFmt.format(d)}`;
}

export function fmtClock(ts) {
  if (!ts) return "—";
  return timeFmt.format(new Date(ts * 1000));
}

export function fmtRelative(ts) {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return fmtTime(ts);
}

export function fmtRange(fromTs, toTs) {
  if (!fromTs || !toTs) return "";
  return `${fmtTime(fromTs)} — ${fmtTime(toTs)}`;
}

export function fmtNumber(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat("zh-CN").format(n);
}

export function fmtCompact(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return fmtNumber(n);
}

export function fmtTokens(value) {
  return value === null || value === undefined ? "未知" : fmtCompact(value);
}

export function fmtDuration(ms) {
  if (ms === null || ms === undefined) return "—";
  const n = Number(ms);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(2)}s`;
  return `${Math.round(n)}ms`;
}

export function fmtMoney(amount, currency) {
  if (amount === null || amount === undefined || amount === "") return "未配置/未知";
  const n = Number(amount);
  if (!Number.isFinite(n)) return `${amount} ${currency || ""}`.trim();
  const digits = Math.abs(n) > 0 && Math.abs(n) < 0.01 ? 6 : 4;
  const text = n.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  });
  return `${text} ${currency || ""}`.trim();
}

export function fmtPercent(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

export function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function statusBadge(status) {
  const map = {
    completed: ["已完成", "green"],
    success: ["成功", "green"],
    running: ["进行中", "accent"],
    error: ["失败", "red"],
    cancelled: ["已取消", "amber"],
    aborted: ["已中止", "amber"],
    incomplete: ["不完整", "amber"],
    unknown: ["未知", "line"],
    estimated: ["已估算", "green"],
    price_not_configured: ["单价未配置", "amber"],
    unknown_usage: ["用量未知", "amber"],
    unknown_billing_mode: ["计费方式未知", "amber"],
    incomplete_price: ["单价不完整", "amber"],
    subscription_or_quota: ["订阅/额度制", "violet"],
    available: ["已提供", "green"],
    not_provided: ["未提供", "line"],
    historical_not_recorded: ["历史未记录", "line"],
    partial: ["部分返回", "amber"],
    expired: ["正文已过期", "amber"],
    generated_not_confirmed: ["模型生成", "violet"],
    sent: ["已进入发送", "green"],
    native_recorded: ["原生记录", "line"],
    received: ["已接收", "accent"],
  };
  const [label, kind] = map[status] || [status || "未知", "line"];
  return { label, kind };
}

export function rangeToTs(range) {
  const now = Date.now() / 1000;
  if (range === "today") {
    const d = new Date();
    const local = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    return { from: local.getTime() / 1000, to: now };
  }
  if (range === "7d") return { from: now - 7 * 86400, to: now };
  return { from: now - 86400, to: now };
}

export function dayLabel(ts) {
  const d = new Date(ts * 1000);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: TZ,
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

export function hourLabel(ts) {
  const d = new Date(ts * 1000);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: TZ,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}
