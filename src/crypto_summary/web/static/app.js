"use strict";

const CURRENCY_SYMBOL = { USD: "$", JPY: "¥", EUR: "€", GBP: "£" };
// 表示通貨ごとの「少額」しきい値（これ以下は初期非表示）
const SMALL_THRESHOLD = { USD: 0.01, EUR: 0.01, GBP: 0.01, JPY: 1 };
// チャート用カラーパレット（紫系をベースに）
const PALETTE = [
  "#8957e5", "#a371f7", "#bc8cff", "#d2b3ff", "#2f81f7",
  "#39c5cf", "#3fb950", "#e3b341", "#f0883e", "#db61a2",
];
const OTHER_COLOR = "#6e7681";

let allocChart = null;
let lastSummary = null;
let showSmall = false;

function fmtMoney(value, currency) {
  const sym = CURRENCY_SYMBOL[currency] || "";
  const n = Number(value);
  const digits = currency === "JPY" ? 2 : 2;
  return sym + n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtAmount(value) {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderWarnings(warnings) {
  const el = document.getElementById("warnings");
  if (!warnings || warnings.length === 0) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = "⚠ " + warnings.map((w) => escapeHtml(w)).join("<br>⚠ ");
}

function isSmall(asset, currency) {
  // 価格なし、または評価額がしきい値以下なら「少額」
  if (!asset.has_price || asset.value === null) return true;
  const th = SMALL_THRESHOLD[currency] ?? 0.01;
  return Math.abs(Number(asset.value)) <= th;
}

/** チャート用スライスを構築（構成比1%未満は「その他」に集約）し、資産→色のマップも返す。 */
function buildChartSlices(priced, total) {
  const slices = [];
  let otherValue = 0;
  const colorByAsset = {};

  priced.forEach((a) => {
    const v = Number(a.value);
    const pct = total > 0 ? (v / total) * 100 : 0;
    if (pct < 1) {
      otherValue += v;
    } else {
      slices.push({ label: a.asset, value: v });
    }
  });

  // 大きい順に色を割り当て
  slices.sort((x, y) => y.value - x.value);
  slices.forEach((s, i) => {
    s.color = PALETTE[i % PALETTE.length];
    colorByAsset[s.label] = s.color;
  });

  if (otherValue > 0) {
    slices.push({ label: "その他", value: otherValue, color: OTHER_COLOR });
  }
  return { slices, colorByAsset };
}

function renderSummary(data) {
  const cur = data.currency;
  const total = Number(data.total_value) || 0;

  document.getElementById("total-value").textContent = fmtMoney(data.total_value, cur);
  document.getElementById("total-sub").textContent =
    `${data.asset_count} 資産 / うち ${data.priced_count} 件に価格あり`;
  document.getElementById("generated").textContent =
    "更新: " + new Date(data.generated_at).toLocaleString();

  const priced = data.assets.filter((a) => a.has_price && a.value !== null);
  const { slices, colorByAsset } = buildChartSlices(priced, total);

  // ---- 資産一覧テーブル ----
  const tbody = document.querySelector("#assets-table tbody");
  tbody.innerHTML = "";
  let hiddenCount = 0;
  data.assets.forEach((a) => {
    const small = isSmall(a, cur);
    if (small) hiddenCount++;
    if (small && !showSmall) return;

    const pct = a.value && total > 0 ? (Number(a.value) / total) * 100 : null;
    const color = colorByAsset[a.asset] || (a.has_price ? OTHER_COLOR : "transparent");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="asset-name"><span class="swatch" style="background:${color}"></span>${escapeHtml(a.asset)}</span></td>
      <td class="num">${fmtAmount(a.balance)}</td>
      <td class="num">${a.price ? fmtMoney(a.price, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${a.value ? fmtMoney(a.value, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
    `;
    tbody.appendChild(tr);
  });

  // ---- 少額残高トグル ----
  const toggleBtn = document.getElementById("toggle-small");
  if (hiddenCount > 0 || showSmall) {
    toggleBtn.style.display = "";
    toggleBtn.textContent = showSmall
      ? "少額残高のトークンを隠す ▴"
      : `少額残高のトークンを表示（${hiddenCount}） ▾`;
  } else {
    toggleBtn.style.display = "none";
  }

  // ---- 価格未取得の資産 ----
  const unpricedEl = document.getElementById("unpriced");
  if (data.unpriced && data.unpriced.length) {
    unpricedEl.classList.remove("hidden");
    unpricedEl.textContent =
      "価格未対応（評価額に未算入）: " + data.unpriced.join(", ");
  } else {
    unpricedEl.classList.add("hidden");
  }

  // ---- 構成比チャート ----
  renderChart(slices, cur, total);
  renderWarnings(data.warnings);
}

/** ドーナツ中央にテキストを描くプラグイン（ホバー時は対象スライスの詳細）。 */
const centerTextPlugin = {
  id: "centerText",
  afterDraw(chart) {
    const { ctx, chartArea } = chart;
    if (!chartArea) return;
    const cx = (chartArea.left + chartArea.right) / 2;
    const cy = (chartArea.top + chartArea.bottom) / 2;
    const cur = chart.$currency;
    const total = chart.$total || 0;

    let title, sub;
    const idx = chart.$activeIndex;
    if (idx != null && chart.data.labels[idx] != null) {
      const val = chart.data.datasets[0].data[idx];
      const pct = total > 0 ? ((val / total) * 100).toFixed(1) + "%" : "";
      title = chart.data.labels[idx];
      sub = fmtMoney(val, cur) + (pct ? `  (${pct})` : "");
    } else {
      title = "合計";
      sub = fmtMoney(total, cur);
    }

    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#8b949e";
    ctx.font = "12px -apple-system, 'Noto Sans JP', sans-serif";
    ctx.fillText(title, cx, cy - 11);
    ctx.fillStyle = "#e6edf3";
    ctx.font = "600 17px -apple-system, 'Noto Sans JP', sans-serif";
    ctx.fillText(sub, cx, cy + 10);
    ctx.restore();
  },
};

function renderChart(slices, currency, total) {
  const ctx = document.getElementById("alloc-chart");

  if (allocChart) allocChart.destroy();
  if (slices.length === 0) {
    ctx.getContext("2d").clearRect(0, 0, ctx.width, ctx.height);
    return;
  }

  allocChart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: slices.map((s) => s.label),
      datasets: [{
        data: slices.map((s) => s.value),
        backgroundColor: slices.map((s) => s.color),
        borderWidth: 0,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "68%",
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },  // 中央表示に置き換え
      },
      onHover(evt, elements) {
        const idx = elements.length ? elements[0].index : null;
        if (allocChart.$activeIndex !== idx) {
          allocChart.$activeIndex = idx;
          allocChart.draw();
        }
      },
    },
    plugins: [centerTextPlugin],
  });
  allocChart.$currency = currency;
  allocChart.$total = total;
  allocChart.$activeIndex = null;
}

function renderSources(data) {
  const cur = data.currency;
  const tbody = document.querySelector("#sources-table tbody");
  tbody.innerHTML = "";
  data.sources.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(s.source)}</td>
      <td class="num">${s.tx_count}</td>
      <td class="num">${fmtMoney(s.total_value, cur)}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function load() {
  const currency = document.getElementById("currency").value;
  const btn = document.getElementById("refresh");
  btn.classList.add("spin");
  document.getElementById("total-sub").textContent = "読み込み中…";
  try {
    const [summary, sources] = await Promise.all([
      fetchJSON(`/api/summary?currency=${currency}`),
      fetchJSON(`/api/sources?currency=${currency}`),
    ]);
    lastSummary = summary;
    renderSummary(summary);
    renderSources(sources);
  } catch (e) {
    document.getElementById("total-sub").textContent = "読み込みエラー: " + e.message;
  } finally {
    btn.classList.remove("spin");
  }
}

document.getElementById("currency").addEventListener("change", () => {
  localStorage.setItem("cs_currency", document.getElementById("currency").value);
  load();
});
document.getElementById("refresh").addEventListener("click", load);
document.getElementById("toggle-small").addEventListener("click", () => {
  showSmall = !showSmall;
  if (lastSummary) renderSummary(lastSummary);  // 再フェッチ不要で再描画
});

// 通貨の選択を復元（デフォルト USD）
const saved = localStorage.getItem("cs_currency");
if (saved) document.getElementById("currency").value = saved;

load();
