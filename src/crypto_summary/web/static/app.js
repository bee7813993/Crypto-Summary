"use strict";

const CURRENCY_SYMBOL = { USD: "$", JPY: "¥", EUR: "€", GBP: "£" };
// チャート用カラーパレット
const PALETTE = [
  "#2f81f7", "#3fb950", "#db61a2", "#e3b341", "#a371f7",
  "#f0883e", "#39c5cf", "#f85149", "#6e7681", "#bc8cff",
];

let allocChart = null;

function fmtMoney(value, currency) {
  const sym = CURRENCY_SYMBOL[currency] || "";
  const n = Number(value);
  const digits = currency === "JPY" ? 0 : 2;
  return sym + n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtAmount(value) {
  const n = Number(value);
  // 大きい数は桁区切り、小さい数は8桁まで
  if (Math.abs(n) >= 1) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 8 });
  }
  return n.toLocaleString(undefined, { maximumFractionDigits: 8, minimumFractionDigits: 0 });
}

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderSummary(data) {
  const cur = data.currency;
  document.getElementById("total-value").textContent = fmtMoney(data.total_value, cur);
  document.getElementById("total-sub").textContent =
    `${data.asset_count} 資産 / うち ${data.priced_count} 件に価格あり`;
  document.getElementById("generated").textContent =
    "更新: " + new Date(data.generated_at).toLocaleString();

  const total = Number(data.total_value) || 1;
  const priced = data.assets.filter((a) => a.has_price);

  // ---- 資産一覧テーブル ----
  const tbody = document.querySelector("#assets-table tbody");
  tbody.innerHTML = "";
  data.assets.forEach((a, i) => {
    const pct = a.value ? (Number(a.value) / total) * 100 : null;
    const color = a.has_price ? PALETTE[priced.indexOf(a) % PALETTE.length] : "transparent";
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
  renderChart(priced, cur);
  renderWarnings(data.warnings);
}

function renderChart(priced, currency) {
  const ctx = document.getElementById("alloc-chart");
  const labels = priced.map((a) => a.asset);
  const values = priced.map((a) => Number(a.value));
  const colors = priced.map((_, i) => PALETTE[i % PALETTE.length]);

  if (allocChart) allocChart.destroy();
  if (priced.length === 0) {
    const c = ctx.getContext("2d");
    c.clearRect(0, 0, ctx.width, ctx.height);
    return;
  }
  allocChart = new Chart(ctx, {
    type: "doughnut",
    data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      plugins: {
        legend: { position: "right", labels: { color: "#8b949e", boxWidth: 12, padding: 10 } },
        tooltip: {
          callbacks: {
            label: (c) => `${c.label}: ${fmtMoney(c.parsed, currency)}`,
          },
        },
      },
    },
  });
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

// 通貨の選択を復元（デフォルト USD）
const saved = localStorage.getItem("cs_currency");
if (saved) document.getElementById("currency").value = saved;

load();
