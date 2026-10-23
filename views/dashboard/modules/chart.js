// Lightweight dependency-free canvas chart: stacked token bars + request line.
// It only renders numbers received from the plugin; no network, no AI.

const PALETTE = ["#b5533a", "#5b6c86", "#8c9c86", "#9a7b3f", "#a24b5f"];

export class TrendChart {
  constructor(container) {
    this.container = container;
    this.canvas = document.createElement("canvas");
    this.tooltip = document.createElement("div");
    this.tooltip.className = "chart-tooltip";
    this.legend = document.createElement("div");
    this.legend.className = "chart-legend";
    container.appendChild(this.legend);
    container.appendChild(this.canvas);
    container.appendChild(this.tooltip);

    this.labels = [];
    this.series = [];
    this.hoverIndex = -1;
    this.ctx = this.canvas.getContext("2d");
    this.metrics = null;

    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);
    this._resize = () => this.draw();
    this.canvas.addEventListener("mousemove", this._onMove);
    this.canvas.addEventListener("mouseleave", this._onLeave);
    this.observer = new ResizeObserver(() => this.draw());
    this.observer.observe(container);
  }

  destroy() {
    this.observer?.disconnect();
    this.canvas.removeEventListener("mousemove", this._onMove);
    this.canvas.removeEventListener("mouseleave", this._onLeave);
    this.container.textContent = "";
  }

  setData({ labels, series }) {
    this.labels = labels || [];
    this.series = (series || []).map((item, index) => ({
      ...item,
      color: item.color || PALETTE[index % PALETTE.length],
    }));
    this._renderLegend();
    this.draw();
  }

  _renderLegend() {
    this.legend.textContent = "";
    for (const item of this.series) {
      const span = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = item.color;
      span.appendChild(swatch);
      span.appendChild(document.createTextNode(item.name));
      this.legend.appendChild(span);
    }
  }

  refresh() {
    this.draw();
  }

  _theme() {
    const style = getComputedStyle(document.documentElement);
    const text = style.getPropertyValue("--text").trim() || "#1f1d18";
    const muted = style.getPropertyValue("--muted").trim() || "#6b6355";
    const border = style.getPropertyValue("--border").trim() || "#e0d9c9";
    const panel = style.getPropertyValue("--panel").trim() || "#fbf9f3";
    const sans = style.getPropertyValue("--sans").trim() || "sans-serif";
    return { text, muted, border, panel, sans };
  }

  _layout() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.container.getBoundingClientRect();
    const width = Math.max(240, rect.width);
    const height = Math.max(160, rect.height || 240);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const pad = { l: 54, r: 46, t: 12, b: 28 };
    return { width, height, pad, plotW: width - pad.l - pad.r, plotH: height - pad.t - pad.b };
  }

  draw() {
    const { ctx } = this;
    if (!ctx) return;
    const { width, height, pad, plotW, plotH } = this._layout();
    const theme = this._theme();
    ctx.clearRect(0, 0, width, height);
    if (!this.labels.length) {
      ctx.fillStyle = theme.muted;
      ctx.font = `12px ${theme.sans}`;
      ctx.textAlign = "center";
      ctx.fillText("暂无数据", width / 2, height / 2);
      this.metrics = null;
      return;
    }

    const barSeries = this.series.filter((item) => item.type !== "line");
    const lineSeries = this.series.filter((item) => item.type === "line");
    const barTotals = this.labels.map((_, index) =>
      barSeries.reduce((sum, item) => sum + Math.max(0, Number(item.values?.[index] || 0)), 0),
    );
    const lineMax = Math.max(1, ...lineSeries.flatMap((item) => (item.values || []).map((v) => Math.max(0, Number(v) || 0))));
    const leftMax = Math.max(1, ...barTotals);
    const stepX = plotW / Math.max(1, this.labels.length);
    const xFor = (index) => pad.l + stepX * (index + 0.5);
    const yLeft = (value) => pad.t + plotH - (value / leftMax) * plotH;
    const yRight = (value) => pad.t + plotH - (value / lineMax) * plotH;

    // Grid + left axis labels.
    ctx.font = `10px ${theme.sans}`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i += 1) {
      const y = pad.t + (plotH / 4) * i;
      ctx.strokeStyle = theme.border;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(width - pad.r, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = theme.muted;
      const value = leftMax - (leftMax / 4) * i;
      ctx.fillText(compact(value), pad.l - 8, y);
      const rightValue = lineMax - (lineMax / 4) * i;
      ctx.textAlign = "left";
      ctx.fillText(compact(rightValue), width - pad.r + 8, y);
      ctx.textAlign = "right";
    }

    // X labels (thinned).
    const maxLabels = Math.max(2, Math.floor(plotW / 64));
    const step = Math.ceil(this.labels.length / maxLabels);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = theme.muted;
    for (let index = 0; index < this.labels.length; index += step) {
      ctx.fillText(this.labels[index], xFor(index), pad.t + plotH + 8);
    }

    // Stacked bars.
    const barW = Math.max(2, Math.min(26, stepX * 0.52));
    this.labels.forEach((_, index) => {
      let base = pad.t + plotH;
      barSeries.forEach((item) => {
        const value = Math.max(0, Number(item.values?.[index] || 0));
        if (!value) return;
        const h = (value / leftMax) * plotH;
        base -= h;
        ctx.fillStyle = item.color;
        ctx.globalAlpha = index === this.hoverIndex ? 1 : 0.86;
        roundRect(ctx, xFor(index) - barW / 2, base, barW, h, Math.min(3, barW / 2));
        ctx.fill();
      });
    });
    ctx.globalAlpha = 1;

    // Request line(s) on right axis.
    for (const item of lineSeries) {
      ctx.strokeStyle = item.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      this.labels.forEach((_, index) => {
        const value = Math.max(0, Number(item.values?.[index] || 0));
        const x = xFor(index);
        const y = yRight(value);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();
      this.labels.forEach((_, index) => {
        const value = Math.max(0, Number(item.values?.[index] || 0));
        ctx.fillStyle = item.color;
        ctx.beginPath();
        ctx.arc(xFor(index), yRight(value), index === this.hoverIndex ? 4 : 2.6, 0, Math.PI * 2);
        ctx.fill();
      });
    }

    // Hover guide.
    if (this.hoverIndex >= 0) {
      const x = xFor(this.hoverIndex);
      ctx.strokeStyle = theme.text;
      ctx.globalAlpha = 0.18;
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, pad.t + plotH);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    this.metrics = { pad, plotW, plotH, stepX, width, height };
  }

  _onMove(event) {
    if (!this.metrics) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const { pad, stepX } = this.metrics;
    const index = Math.floor((x - pad.l) / stepX);
    if (index < 0 || index >= this.labels.length) {
      this._onLeave();
      return;
    }
    if (index !== this.hoverIndex) {
      this.hoverIndex = index;
      this.draw();
    }
    const rows = this.series
      .map(
        (item) =>
          `<div class="tt-row"><span><i class="swatch" style="background:${item.color};display:inline-block;margin-right:5px"></i>${escapeHtml(
            item.name,
          )}</span><b>${formatValue(item.values?.[index])}</b></div>`,
      )
      .join("");
    this.tooltip.innerHTML = `<div class="tt-title">${escapeHtml(this.labels[index])}</div>${rows}`;
    this.tooltip.style.opacity = "1";
    const left = Math.min(Math.max(8, event.clientX - rect.left + 12), rect.width - 170);
    const top = Math.max(8, event.clientY - rect.top - 10);
    this.tooltip.style.left = `${left}px`;
    this.tooltip.style.top = `${top}px`;
  }

  _onLeave() {
    this.hoverIndex = -1;
    this.tooltip.style.opacity = "0";
    this.draw();
  }
}

function compact(value) {
  const n = Number(value) || 0;
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(n);
}

function formatValue(value) {
  const n = Number(value) || 0;
  return new Intl.NumberFormat("zh-CN").format(n);
}

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, Math.abs(w) / 2, Math.abs(h));
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}
