"use strict";

const CURRENCY_SYMBOL = { USD: "$", JPY: "¥", EUR: "€", GBP: "£" };
const SMALL_THRESHOLD = { USD: 0.01, EUR: 0.01, GBP: 0.01, JPY: 1 };
const PALETTE = [
  "#8957e5", "#a371f7", "#bc8cff", "#d2b3ff", "#2f81f7",
  "#39c5cf", "#3fb950", "#e3b341", "#f0883e", "#db61a2",
];
const OTHER_COLOR = "#6e7681";

let allocChart = null;
let lastSummary = null;
let showSmall = false;

// ---- ユーティリティ ----

function fmtMoney(value, currency) {
  const sym = CURRENCY_SYMBOL[currency] || "";
  const n = Number(value);
  return sym + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtAmount(value) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 8 });
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

// ---- ページナビゲーション ----

const PAGES = ["dashboard", "accounts", "assets"];

function showPage(name) {
  if (!PAGES.includes(name)) name = "dashboard";

  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== name);
  });

  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });

  // ページ切り替え時はドリルダウンをリセット
  if (name === "accounts") {
    showAccountsList();
    loadAccountsPage();
  } else if (name === "assets") {
    showAssetsList();
    loadAssetsPage();
  }
}

document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const page = a.dataset.page;
    history.pushState({ page }, "", `#${page}`);
    showPage(page);
  });
});

window.addEventListener("popstate", (e) => {
  showPage((e.state && e.state.page) || "dashboard");
});

/** ページのナビ状態だけ切り替える（list/detail のリセットはしない）。 */
function activatePage(name) {
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== name);
  });
  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });
  history.pushState({ page: name }, "", `#${name}`);
}

/** ダッシュボードの資産クリック → 資産別ページの口座内訳へ直接遷移。 */
function navigateToAssetDetail(symbol) {
  activatePage("assets");
  showAssetDetail(symbol);
}

/** ダッシュボードの口座クリック → 口座別ページの資産内訳へ直接遷移。 */
function navigateToAccountDetail(name) {
  activatePage("accounts");
  showAccountDetail(name);
}

// ---- ダッシュボード ----

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
  if (!asset.has_price || asset.value === null) return true;
  const th = SMALL_THRESHOLD[currency] ?? 0.01;
  return Math.abs(Number(asset.value)) <= th;
}

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
    tr.className = "clickable";
    tr.innerHTML = `
      <td><span class="asset-name"><span class="swatch" style="background:${color}"></span>${escapeHtml(a.asset)} <span class="row-arrow">›</span></span></td>
      <td class="num">${fmtAmount(a.balance)}</td>
      <td class="num">${a.price ? fmtMoney(a.price, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${a.value ? fmtMoney(a.value, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
    `;
    // クリックで資産別ページの口座内訳ドリルダウンへ
    tr.addEventListener("click", () => navigateToAssetDetail(a.asset));
    tbody.appendChild(tr);
  });

  // 円グラフ下の凡例（資産 / 比率 / 評価額）
  renderLegend(slices, cur, total);

  const toggleBtn = document.getElementById("toggle-small");
  if (hiddenCount > 0 || showSmall) {
    toggleBtn.style.display = "";
    toggleBtn.textContent = showSmall
      ? "少額残高のトークンを隠す ▴"
      : `少額残高のトークンを表示（${hiddenCount}） ▾`;
  } else {
    toggleBtn.style.display = "none";
  }

  const unpricedEl = document.getElementById("unpriced");
  if (data.unpriced && data.unpriced.length) {
    unpricedEl.classList.remove("hidden");
    unpricedEl.textContent = "価格未対応（評価額に未算入）: " + data.unpriced.join(", ");
  } else {
    unpricedEl.classList.add("hidden");
  }

  renderChart(slices, cur, total);
  renderWarnings(data.warnings);
}

function renderSources(data) {
  const cur = data.currency;
  const tbody = document.querySelector("#sources-table tbody");
  tbody.innerHTML = "";
  data.sources.forEach((s) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `
      <td>${escapeHtml(s.source)} <span class="row-arrow">›</span></td>
      <td class="num">${s.tx_count}</td>
      <td class="num">${fmtMoney(s.total_value, cur)}</td>
    `;
    // クリックで口座別ページの資産内訳ドリルダウンへ
    tr.addEventListener("click", () => navigateToAccountDetail(s.source));
    tbody.appendChild(tr);
  });
}

/** 円グラフ下に凡例（資産 / 比率 / 評価額）を描画。ホバーで中央表示と連動。 */
function renderLegend(slices, currency, total) {
  const el = document.getElementById("chart-legend");
  el.innerHTML = "";
  slices.forEach((s, i) => {
    const pct = total > 0 ? (s.value / total) * 100 : 0;
    const row = document.createElement("div");
    row.className = "legend-item";
    row.innerHTML = `
      <span class="legend-swatch" style="background:${s.color}"></span>
      <span class="legend-name">${escapeHtml(s.label)}</span>
      <span class="legend-pct">${pct.toFixed(1)}%</span>
      <span class="legend-value">${fmtMoney(s.value, currency)}</span>
    `;
    // 凡例ホバー → 円グラフ中央に該当スライスの詳細を表示
    row.addEventListener("mouseenter", () => setChartActive(i));
    row.addEventListener("mouseleave", () => setChartActive(null));
    el.appendChild(row);
  });
}

function setChartActive(idx) {
  if (!allocChart) return;
  if (allocChart.$activeIndex !== idx) {
    allocChart.$activeIndex = idx;
    allocChart.draw();
  }
}

// ---- チャート ----

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
        tooltip: { enabled: false },
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

// ---- 口座別ページ ----

function showAccountsList() {
  document.getElementById("accounts-list-view").classList.remove("hidden");
  document.getElementById("account-detail-view").classList.add("hidden");
}

function showAccountDetail(name) {
  document.getElementById("accounts-list-view").classList.add("hidden");
  const detail = document.getElementById("account-detail-view");
  detail.classList.remove("hidden");
  document.getElementById("account-detail-name").textContent = name;
  loadAccountDetail(name);
}

async function loadAccountsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#accounts-table tbody");
  tbody.innerHTML = '<tr><td colspan="4" class="loading">読み込み中…</td></tr>';
  try {
    const data = await fetchJSON(`/api/sources?currency=${currency}`);
    tbody.innerHTML = "";
    data.sources.forEach((s) => {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `
        <td>${escapeHtml(s.source)} <span class="row-arrow">›</span></td>
        <td class="num">${s.tx_count}</td>
        <td class="num">${s.asset_count}</td>
        <td class="num">${fmtMoney(s.total_value, currency)}</td>
      `;
      tr.addEventListener("click", () => showAccountDetail(s.source));
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadAccountDetail(name) {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#account-assets-table tbody");
  const loading = document.getElementById("account-detail-loading");
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(`/api/account-assets?account=${encodeURIComponent(name)}&currency=${currency}`);
    loading.classList.add("hidden");
    data.assets.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.asset)}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
      `;
      tbody.appendChild(tr);
    });
    if (data.assets.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">残高なし</td></tr>';
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="4" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- 資産別ページ ----

function showAssetsList() {
  document.getElementById("assets-list-view").classList.remove("hidden");
  document.getElementById("asset-detail-view").classList.add("hidden");
}

function showAssetDetail(symbol) {
  document.getElementById("assets-list-view").classList.add("hidden");
  const detail = document.getElementById("asset-detail-view");
  detail.classList.remove("hidden");
  document.getElementById("asset-detail-name").textContent = symbol;
  loadAssetDetail(symbol);
}

async function loadAssetsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#all-assets-table tbody");
  tbody.innerHTML = '<tr><td colspan="5" class="loading">読み込み中…</td></tr>';
  try {
    const data = await fetchJSON(`/api/summary?currency=${currency}`);
    const total = Number(data.total_value) || 0;
    tbody.innerHTML = "";
    data.assets.forEach((a) => {
      const pct = a.value && total > 0 ? (Number(a.value) / total) * 100 : null;
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `
        <td>${escapeHtml(a.asset)} <span class="row-arrow">›</span></td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
      `;
      tr.addEventListener("click", () => showAssetDetail(a.asset));
      tbody.appendChild(tr);
    });
    if (data.assets.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">データなし</td></tr>';
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadAssetDetail(symbol) {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#asset-accounts-table tbody");
  const loading = document.getElementById("asset-detail-loading");
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(`/api/asset-accounts?asset=${encodeURIComponent(symbol)}&currency=${currency}`);
    loading.classList.add("hidden");
    data.accounts.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.account)}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
      `;
      tbody.appendChild(tr);
    });
    if (data.accounts.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="muted">保有口座なし</td></tr>';
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="3" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- メインロード ----

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

// ---- イベントリスナー ----

document.getElementById("currency").addEventListener("change", () => {
  localStorage.setItem("cs_currency", document.getElementById("currency").value);
  // 現在表示中のページを再ロード
  const cur = getCurrentPage();
  if (cur === "dashboard") load();
  else if (cur === "accounts") loadAccountsPage();
  else if (cur === "assets") loadAssetsPage();
});

document.getElementById("refresh").addEventListener("click", () => {
  const cur = getCurrentPage();
  if (cur === "dashboard") load();
  else if (cur === "accounts") loadAccountsPage();
  else if (cur === "assets") loadAssetsPage();
});

document.getElementById("toggle-small").addEventListener("click", () => {
  showSmall = !showSmall;
  if (lastSummary) renderSummary(lastSummary);
});

document.getElementById("account-back").addEventListener("click", showAccountsList);
document.getElementById("asset-back").addEventListener("click", showAssetsList);

function getCurrentPage() {
  const active = document.querySelector(".nav-link.active[data-page]");
  return active ? active.dataset.page : "dashboard";
}

// ---- 初期化 ----

const saved = localStorage.getItem("cs_currency");
if (saved) document.getElementById("currency").value = saved;

// ハッシュに基づいて初期ページを決定
const initPage = location.hash.replace("#", "") || "dashboard";
showPage(initPage);
if (initPage === "dashboard") load();
