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

function fmtDate(iso) {
  return new Date(iso).toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
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

const PAGES = ["dashboard", "accounts", "assets", "transactions"];

function showPage(name) {
  if (!PAGES.includes(name)) name = "dashboard";

  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== name);
  });
  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });

  if (name === "accounts") { showAccountsList(); loadAccountsPage(); }
  else if (name === "assets") { showAssetsList(); loadAssetsPage(); }
  else if (name === "transactions") { /* filters set by caller */ }
}

document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    const page = a.dataset.page;
    if (page === "transactions") {
      navigateToTransactions({});
    } else {
      history.pushState({ page }, "", `#${page}`);
      showPage(page);
    }
  });
});

window.addEventListener("popstate", (e) => {
  const state = e.state || {};
  showPage(state.page || "dashboard");
  if (state.page === "transactions") {
    loadTransactionsPage(state.txAccount || null, state.txAsset || null, state.txPage || 1);
  }
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
}

/** ダッシュボードの資産クリック → 資産別ページの口座内訳へ直接遷移。 */
function navigateToAssetDetail(symbol) {
  history.pushState({ page: "assets" }, "", "#assets");
  activatePage("assets");
  showAssetDetail(symbol);
}

/** ダッシュボードの口座クリック → 口座別ページの資産内訳へ直接遷移。 */
function navigateToAccountDetail(name) {
  history.pushState({ page: "accounts" }, "", "#accounts");
  activatePage("accounts");
  showAccountDetail(name);
}

/** 取引履歴ページへ遷移（フィルタ付き）。 */
function navigateToTransactions({ account = null, asset = null, page = 1 } = {}) {
  const state = { page: "transactions", txAccount: account, txAsset: asset, txPage: page };
  history.pushState(state, "", "#transactions");
  activatePage("transactions");
  loadTransactionsPage(account, asset, page);
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
    tr.innerHTML = `
      <td class="clickable-cell" data-action="asset-detail">
        <span class="asset-name">
          <span class="swatch" style="background:${color}"></span>${escapeHtml(a.asset)}
        </span>
      </td>
      <td class="num">${fmtAmount(a.balance)}</td>
      <td class="num">${a.price ? fmtMoney(a.price, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${a.value ? fmtMoney(a.value, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
      <td><button class="tx-link-btn" data-asset="${escapeHtml(a.asset)}">≡ 履歴</button></td>
    `;
    tr.querySelector("[data-action='asset-detail']").addEventListener("click", () =>
      navigateToAssetDetail(a.asset));
    tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      navigateToTransactions({ asset: a.asset });
    });
    tbody.appendChild(tr);
  });

  // 少額トグル
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
  renderLegend(slices, cur, total);
  renderWarnings(data.warnings);
}

function renderSources(data) {
  const cur = data.currency;
  const tbody = document.querySelector("#sources-table tbody");
  tbody.innerHTML = "";
  data.sources.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="clickable-cell" data-action="account-detail">
        ${escapeHtml(s.source)} <span class="row-arrow">›</span>
      </td>
      <td class="num">${s.tx_count}</td>
      <td class="num">${fmtMoney(s.total_value, cur)}</td>
      <td><button class="tx-link-btn" data-account="${escapeHtml(s.source)}">≡ 履歴</button></td>
    `;
    tr.querySelector("[data-action='account-detail']").addEventListener("click", () =>
      navigateToAccountDetail(s.source));
    tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      navigateToTransactions({ account: s.source });
    });
    tbody.appendChild(tr);
  });
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

/** 円グラフ下の凡例（資産 / 比率 / 評価額）。凡例ホバーで中央表示と連動。 */
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
  // 取引履歴ボタンに口座名をセット
  document.getElementById("account-tx-link").onclick = () =>
    navigateToTransactions({ account: name });
  loadAccountDetail(name);
}

async function loadAccountsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#accounts-table tbody");
  tbody.innerHTML = '<tr><td colspan="5" class="loading">読み込み中…</td></tr>';
  try {
    const data = await fetchJSON(`/api/sources?currency=${currency}`);
    tbody.innerHTML = "";
    data.sources.forEach((s) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="clickable-cell" data-action="account-detail">
          ${escapeHtml(s.source)} <span class="row-arrow">›</span>
        </td>
        <td class="num">${s.tx_count}</td>
        <td class="num">${s.asset_count}</td>
        <td class="num">${fmtMoney(s.total_value, currency)}</td>
        <td><button class="tx-link-btn">≡ 履歴</button></td>
      `;
      tr.querySelector("[data-action='account-detail']").addEventListener("click", () =>
        showAccountDetail(s.source));
      tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        navigateToTransactions({ account: s.source });
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadAccountDetail(name) {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#account-assets-table tbody");
  const loading = document.getElementById("account-detail-loading");
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/account-assets?account=${encodeURIComponent(name)}&currency=${currency}`
    );
    loading.classList.add("hidden");
    data.assets.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.asset)}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
        <td><button class="tx-link-btn">≡ 履歴</button></td>
      `;
      // 口座内の資産行 → 口座×資産の取引履歴
      tr.querySelector(".tx-link-btn").addEventListener("click", () =>
        navigateToTransactions({ account: name, asset: a.asset }));
      tbody.appendChild(tr);
    });
    if (data.assets.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">残高なし</td></tr>';
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="5" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
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
  // 取引履歴ボタンに資産名をセット
  document.getElementById("asset-tx-link").onclick = () =>
    navigateToTransactions({ asset: symbol });
  loadAssetDetail(symbol);
}

async function loadAssetsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#all-assets-table tbody");
  tbody.innerHTML = '<tr><td colspan="6" class="loading">読み込み中…</td></tr>';
  try {
    const data = await fetchJSON(`/api/summary?currency=${currency}`);
    const total = Number(data.total_value) || 0;
    tbody.innerHTML = "";
    data.assets.forEach((a) => {
      const pct = a.value && total > 0 ? (Number(a.value) / total) * 100 : null;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="clickable-cell" data-action="asset-detail">
          ${escapeHtml(a.asset)} <span class="row-arrow">›</span>
        </td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
        <td><button class="tx-link-btn">≡ 履歴</button></td>
      `;
      tr.querySelector("[data-action='asset-detail']").addEventListener("click", () =>
        showAssetDetail(a.asset));
      tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        navigateToTransactions({ asset: a.asset });
      });
      tbody.appendChild(tr);
    });
    if (data.assets.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">データなし</td></tr>';
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadAssetDetail(symbol) {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#asset-accounts-table tbody");
  const loading = document.getElementById("asset-detail-loading");
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/asset-accounts?asset=${encodeURIComponent(symbol)}&currency=${currency}`
    );
    loading.classList.add("hidden");
    data.accounts.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(a.account)}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
        <td><button class="tx-link-btn">≡ 履歴</button></td>
      `;
      // 資産内の口座行 → 口座×資産の取引履歴
      tr.querySelector(".tx-link-btn").addEventListener("click", () =>
        navigateToTransactions({ account: a.account, asset: symbol }));
      tbody.appendChild(tr);
    });
    if (data.accounts.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">保有口座なし</td></tr>';
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="4" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- 取引履歴ページ ----

let _txAccountOptions = [];  // フィルタ用口座リスト（初回ロード時に取得）

async function ensureTxFilterOptions() {
  if (_txAccountOptions.length) return;
  try {
    const data = await fetchJSON("/api/sources?currency=USD");
    _txAccountOptions = data.sources.map((s) => s.source);
    const sel = document.getElementById("tx-filter-account");
    _txAccountOptions.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
  } catch (_) { /* サイレント */ }
}

async function loadTransactionsPage(account, asset, page = 1) {
  await ensureTxFilterOptions();

  // フィルタUIを同期
  const selAccount = document.getElementById("tx-filter-account");
  const selAsset = document.getElementById("tx-filter-asset");
  selAccount.value = account || "";
  if (asset) {
    // asset の option が未追加なら追加
    if (![...selAsset.options].some((o) => o.value === asset)) {
      const opt = document.createElement("option");
      opt.value = asset; opt.textContent = asset;
      selAsset.appendChild(opt);
    }
    selAsset.value = asset;
  } else {
    selAsset.value = "";
  }

  // アクティブフィルター表示
  const banner = document.getElementById("tx-active-filter");
  const parts = [];
  if (account) parts.push(`口座: <strong>${escapeHtml(account)}</strong>`);
  if (asset) parts.push(`資産: <strong>${escapeHtml(asset)}</strong>`);
  if (parts.length) {
    banner.innerHTML = "絞り込み中 — " + parts.join(" ／ ");
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  const tbody = document.querySelector("#tx-table tbody");
  const loading = document.getElementById("tx-loading");
  const empty = document.getElementById("tx-empty");
  tbody.innerHTML = "";
  loading.classList.remove("hidden");
  empty.classList.add("hidden");

  try {
    let url = `/api/transactions?page=${page}`;
    if (account) url += `&account=${encodeURIComponent(account)}`;
    if (asset) url += `&asset=${encodeURIComponent(asset)}`;

    const data = await fetchJSON(url);
    loading.classList.add("hidden");

    if (data.transactions.length === 0) {
      empty.classList.remove("hidden");
    } else {
      data.transactions.forEach((tx) => {
        const tr = document.createElement("tr");
        const recv = tx.received_asset
          ? `${fmtAmount(tx.received_amount)} ${escapeHtml(tx.received_asset)}`
          : '<span class="muted">-</span>';
        const sent = tx.sent_asset
          ? `${fmtAmount(tx.sent_amount)} ${escapeHtml(tx.sent_asset)}`
          : '<span class="muted">-</span>';
        const fee = tx.fee_asset
          ? `${fmtAmount(tx.fee_amount)} ${escapeHtml(tx.fee_asset)}`
          : '<span class="muted">-</span>';
        const hash = tx.tx_hash
          ? `<span class="tx-hash" title="${escapeHtml(tx.tx_hash)}">${escapeHtml(tx.tx_hash)}</span>`
          : "";
        tr.innerHTML = `
          <td style="white-space:nowrap">${fmtDate(tx.timestamp)}</td>
          <td>${escapeHtml(tx.account)}</td>
          <td><span class="tx-type tx-type-${escapeHtml(tx.type)}">${escapeHtml(tx.type_ja)}</span></td>
          <td>${recv}</td>
          <td>${sent}</td>
          <td>${fee}</td>
          <td class="muted">${tx.label ? escapeHtml(tx.label) : ""}${hash}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    renderTxPagination(data, account, asset);
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="7" class="muted">エラー: ${escapeHtml(e.message)}</td></tr>`;
    document.getElementById("tx-pagination").innerHTML = "";
  }
}

function renderTxPagination(data, account, asset) {
  const el = document.getElementById("tx-pagination");
  el.innerHTML = "";
  if (data.total_pages <= 1) return;

  const cur = data.page;
  const total = data.total_pages;

  // 前へ
  if (cur > 1) {
    const btn = document.createElement("button");
    btn.textContent = "‹";
    btn.addEventListener("click", () => navigateToTransactions({ account, asset, page: cur - 1 }));
    el.appendChild(btn);
  }

  // ページ番号（最大7個表示）
  const pages = pageRange(cur, total);
  pages.forEach((p) => {
    if (p === "…") {
      const span = document.createElement("span");
      span.className = "page-info";
      span.textContent = "…";
      el.appendChild(span);
    } else {
      const btn = document.createElement("button");
      btn.textContent = p;
      if (p === cur) btn.classList.add("active");
      btn.addEventListener("click", () => navigateToTransactions({ account, asset, page: p }));
      el.appendChild(btn);
    }
  });

  // 次へ
  if (cur < total) {
    const btn = document.createElement("button");
    btn.textContent = "›";
    btn.addEventListener("click", () => navigateToTransactions({ account, asset, page: cur + 1 }));
    el.appendChild(btn);
  }

  const info = document.createElement("span");
  info.className = "page-info";
  info.textContent = `${data.total.toLocaleString()} 件`;
  el.appendChild(info);
}

function pageRange(cur, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = [1];
  if (cur > 3) pages.push("…");
  for (let p = Math.max(2, cur - 1); p <= Math.min(total - 1, cur + 1); p++) pages.push(p);
  if (cur < total - 2) pages.push("…");
  pages.push(total);
  return pages;
}

// ---- フィルタUI変更 ----

document.getElementById("tx-filter-account").addEventListener("change", (e) => {
  const account = e.target.value || null;
  const asset = document.getElementById("tx-filter-asset").value || null;
  navigateToTransactions({ account, asset, page: 1 });
});

document.getElementById("tx-filter-asset").addEventListener("change", (e) => {
  const asset = e.target.value || null;
  const account = document.getElementById("tx-filter-account").value || null;
  navigateToTransactions({ account, asset, page: 1 });
});

document.getElementById("tx-filter-clear").addEventListener("click", () => {
  navigateToTransactions({});
});

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
    // 口座フィルタ選択肢も更新
    _txAccountOptions = [];
    const sel = document.getElementById("tx-filter-account");
    [...sel.options].forEach((o) => { if (o.value) o.remove(); });
  } catch (e) {
    document.getElementById("total-sub").textContent = "読み込みエラー: " + e.message;
  } finally {
    btn.classList.remove("spin");
  }
}

function getCurrentPage() {
  const active = document.querySelector(".nav-link.active[data-page]");
  return active ? active.dataset.page : "dashboard";
}

// ---- イベントリスナー ----

document.getElementById("currency").addEventListener("change", () => {
  localStorage.setItem("cs_currency", document.getElementById("currency").value);
  const cur = getCurrentPage();
  if (cur === "dashboard") load();
  else if (cur === "accounts") loadAccountsPage();
  else if (cur === "assets") loadAssetsPage();
  // 取引履歴は通貨フィルタ不要のためそのまま
});

document.getElementById("refresh").addEventListener("click", () => {
  const cur = getCurrentPage();
  if (cur === "dashboard") load();
  else if (cur === "accounts") loadAccountsPage();
  else if (cur === "assets") loadAssetsPage();
  else if (cur === "transactions") {
    const account = document.getElementById("tx-filter-account").value || null;
    const asset = document.getElementById("tx-filter-asset").value || null;
    loadTransactionsPage(account, asset, 1);
  }
});

document.getElementById("toggle-small").addEventListener("click", () => {
  showSmall = !showSmall;
  if (lastSummary) renderSummary(lastSummary);
});

document.getElementById("account-back").addEventListener("click", showAccountsList);
document.getElementById("asset-back").addEventListener("click", showAssetsList);

// ---- 初期化 ----

const saved = localStorage.getItem("cs_currency");
if (saved) document.getElementById("currency").value = saved;

const initHash = location.hash.replace("#", "") || "dashboard";
const initPage = PAGES.includes(initHash) ? initHash : "dashboard";
showPage(initPage);
if (initPage === "dashboard") load();
else if (initPage === "transactions") loadTransactionsPage(null, null, 1);
