"use strict";

// ---- テーマ初期化 ----
// 金額マスクは描画時に fmtMoney/fmtAmount が maskAmounts を参照するため、
// ここでクラス付与等の初期化は不要（フラグは宣言時に localStorage から復元）。
(function initPrefs() {
  if (localStorage.getItem("cs_theme") === "light") {
    document.documentElement.classList.add("light");
  }
})();

const CURRENCY_SYMBOL = { USD: "$", JPY: "¥", EUR: "€", GBP: "£" };
const SMALL_THRESHOLD = { USD: 0.01, EUR: 0.01, GBP: 0.01, JPY: 1 };
const PALETTE = [
  "#8957e5", "#a371f7", "#bc8cff", "#d2b3ff", "#2f81f7",
  "#39c5cf", "#3fb950", "#e3b341", "#f0883e", "#db61a2",
];
const OTHER_COLOR = "#6e7681";

const DASH_TOP = 5; // ダッシュボードのプレビュー件数（全件は専用ページ）

let allocChart = null;
let lastAssetsData = null; // /api/summary の最新結果（資産別ページの再描画用）
let showSmall = false;
let maskAmounts = localStorage.getItem("cs_mask") === "1";

// 推移グラフのインスタンスと状態
// 暗号資産アイコンURL（/api/coin-icons から取得してキャッシュ）
let _coinIcons = {};
async function _loadCoinIcons() {
  try {
    _coinIcons = await fetchJSON("/api/coin-icons");
  } catch (_) { /* ネットワーク失敗時はアイコンなしで続行 */ }
}

let _histChart = null;
let _acctHistChart = null;
let _assetHistChart = null;
let _dashHistRange = localStorage.getItem("cs_dash_range") || "90d";
let _acctHistRange = localStorage.getItem("cs_acct_range") || "90d";
let _assetHistRange = localStorage.getItem("cs_asset_range") || "90d";
let _acctHistName = null;
let _assetHistSymbol = null;

// ---- ユーティリティ ----

function fmtMoney(value, currency) {
  if (maskAmounts) return (CURRENCY_SYMBOL[currency] || "") + "●●●●●";
  const sym = CURRENCY_SYMBOL[currency] || "";
  const n = Number(value);
  return sym + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// JPY 専用: 億・万・円 単位のサブ表示用
function fmtJpy(value) {
  if (maskAmounts) return "¥●●●●●";
  const n = Math.round(Number(value));
  if (n === 0) return "0円";
  const oku = Math.floor(n / 100_000_000);
  const manPart = n % 100_000_000;
  const man = Math.floor(manPart / 10_000);
  const yen = manPart % 10_000;
  let str = "";
  if (oku > 0) str += oku + "億";
  if (man > 0) str += man + "万";
  if (yen > 0 || str === "") str += yen + "円";
  else str += "円";
  return str;
}

// 通貨セレクトのオプションに完全名を付与する
const _CURRENCY_LABELS = {
  ja: { USD: "USD　米ドル", JPY: "JPY　日本円", EUR: "EUR　ユーロ", GBP: "GBP　英ポンド" },
  en: { USD: "USD  US Dollar", JPY: "JPY  Japanese Yen", EUR: "EUR  Euro", GBP: "GBP  Pound Sterling" },
};
function _syncCurrencyLabels() {
  const sel = document.getElementById("currency");
  if (!sel) return;
  const labels = _CURRENCY_LABELS[_lang] || _CURRENCY_LABELS.ja;
  [...sel.options].forEach((opt) => {
    opt.textContent = labels[opt.value] || opt.value;
  });
}

function coinIconHtml(symbol) {
  const url = _coinIcons[symbol.toUpperCase()];
  if (!url) return `<span class="coin-icon-fallback">${escapeHtml(symbol.charAt(0))}</span>`;
  return `<img class="coin-icon" src="${escapeHtml(url)}" alt="${escapeHtml(symbol)}" loading="lazy">`;
}

function assetNameHtml(symbol, swatchHtml = "") {
  return `<span class="asset-name">${swatchHtml}${coinIconHtml(symbol)}<span class="asset-label">${escapeHtml(symbol)}</span></span>`;
}

function fmtAmount(value) {
  if (maskAmounts) return "●●●●●";
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

// 取引種別の表示名。英語表示時は txtype.* で翻訳し、日本語時はバックエンド由来の type_ja を使う。
function _txTypeLabel(tx) {
  if (_lang === "ja") return tx.type_ja || tx.type;
  const key = "txtype." + tx.type;
  const translated = t(key);
  return translated === key ? (tx.type_ja || tx.type) : translated;
}

function chartTheme() {
  const s = getComputedStyle(document.documentElement);
  const g = (v) => s.getPropertyValue(v).trim();
  return {
    tick: g("--text-dim"),
    grid: g("--border"),
    tooltipBg: g("--bg-elev"),
    tooltipBorder: g("--border"),
    tooltipTitle: g("--text"),
    tooltipBody: g("--text-dim"),
  };
}

// ---- テーマ切替 ----

function _syncThemeBtn() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const isLight = document.documentElement.classList.contains("light");
  btn.textContent = isLight ? "🌙" : "☀";
  btn.title = isLight ? t("toggle.toDark") : t("toggle.toLight");
}

document.getElementById("theme-toggle").addEventListener("click", () => {
  const isLight = document.documentElement.classList.toggle("light");
  localStorage.setItem("cs_theme", isLight ? "light" : "dark");
  _syncThemeBtn();
  router();
});

// ---- 金額マスク切替 ----

function _syncMaskBtn() {
  const btn = document.getElementById("mask-toggle");
  if (!btn) return;
  btn.textContent = maskAmounts ? "🔒" : "👁";
  btn.title = maskAmounts ? t("toggle.maskOff") : t("toggle.maskOn");
}

// マスクモードのときはサイドバーのGoogleアカウント情報（名前・メール・
// アバター）をぼかして個人情報を隠す。
function _syncUserMask() {
  const info = document.getElementById("user-info");
  if (!info) return;
  info.classList.toggle("masked", maskAmounts);
}

function _syncLangBtn() {
  const btn = document.getElementById("lang-toggle");
  if (!btn) return;
  btn.textContent = t("toggle.langBtn");
  btn.title = t("toggle.langTitle");
}

document.getElementById("lang-toggle").addEventListener("click", () => {
  setLang(_lang === "ja" ? "en" : "ja");
  _syncThemeBtn();
  _syncMaskBtn();
  _syncLangBtn();
  _syncCurrencyLabels();
  router();
});

document.getElementById("mask-toggle").addEventListener("click", () => {
  maskAmounts = !maskAmounts;
  localStorage.setItem("cs_mask", maskAmounts ? "1" : "0");
  _syncMaskBtn();
  _syncUserMask();
  // HTML の金額表示・グラフのテキストを再描画して一括で反映する。
  router();
});

// ---- ページナビゲーション ----

const PAGES = ["dashboard", "accounts", "assets", "transactions", "import"];

// ---- ハッシュルーター ----
// URL ハッシュに状態を全て持たせ、リロード・ブックマーク・進む/戻るで復元可能にする。
//   #dashboard
//   #accounts                         （口座一覧）
//   #accounts/detail?name=bitFlyer    （口座詳細）
//   #assets                           （資産一覧）
//   #assets/detail?name=BTC           （資産詳細）
//   #transactions?account=..&asset=..&since=..&until=..&page=2
//   #import

function _encodeParams(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj || {})) {
    if (v != null && v !== "") p.set(k, v);
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

function buildHash(page, sub, params) {
  let h = page;
  if (sub) h += "/" + sub;
  h += _encodeParams(params);
  return h;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return { page: "dashboard", sub: null, params: {} };
  const qIdx = raw.indexOf("?");
  const path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
  const query = qIdx >= 0 ? raw.slice(qIdx + 1) : "";
  const [page, sub] = path.split("/");
  const params = {};
  new URLSearchParams(query).forEach((v, k) => { params[k] = v; });
  return { page: PAGES.includes(page) ? page : "dashboard", sub: sub || null, params };
}

// 現在の URL ハッシュを読み取って画面を描画する（唯一の描画起点）。
function router() {
  const { page, sub, params } = parseHash();
  activatePage(page);

  if (page === "dashboard") {
    load();
  } else if (page === "accounts") {
    if (sub === "detail" && params.name) {
      showAccountDetail(params.name);
    } else {
      showAccountsList();
      loadAccountsPage();
    }
  } else if (page === "assets") {
    if (sub === "detail" && params.name) {
      showAssetDetail(params.name);
    } else {
      showAssetsList();
      loadAssetsPage();
    }
  } else if (page === "transactions") {
    loadTransactionsPage(
      params.account || null, params.asset || null,
      params.since || null, params.until || null,
      Number(params.page) || 1,
    );
  } else if (page === "import") {
    loadImportPage();
  }
}

// ハッシュを更新して描画する（pushState は popstate を発火しないので明示的に router を呼ぶ）。
function navigate(page, sub = null, params = null) {
  history.pushState(null, "", "#" + buildHash(page, sub, params));
  router();
}

document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    navigate(a.dataset.page);
  });
});

// 進む/戻るは URL から状態を再構築する。
window.addEventListener("popstate", router);

function activatePage(name) {
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== name);
  });
  document.querySelectorAll(".nav-link[data-page]").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });
}

function navigateToAssetDetail(symbol) {
  navigate("assets", "detail", { name: symbol });
}

function navigateToAccountDetail(name) {
  navigate("accounts", "detail", { name });
}

function navigateToTransactions({ account = null, asset = null, since = null, until = null, page = 1 } = {}) {
  navigate("transactions", null, { account, asset, since, until, page: page > 1 ? page : null });
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
      slices.push({ label: a.asset, value: v, balance: a.balance });
    }
  });

  slices.sort((x, y) => y.value - x.value);
  slices.forEach((s, i) => {
    s.color = PALETTE[i % PALETTE.length];
    colorByAsset[s.label] = s.color;
  });

  if (otherValue > 0) {
    slices.push({ label: t("label.other"), value: otherValue, color: OTHER_COLOR });
  }
  return { slices, colorByAsset };
}

function renderSummary(data) {
  const cur = data.currency;
  const total = Number(data.total_value) || 0;

  document.getElementById("total-value").textContent = fmtMoney(data.total_value, cur);
  const jpyEl = document.getElementById("total-jpy");
  if (jpyEl) {
    if (cur === "JPY") {
      jpyEl.textContent = fmtJpy(data.total_value);
      jpyEl.classList.remove("hidden");
    } else {
      jpyEl.classList.add("hidden");
    }
  }
  document.getElementById("total-sub").textContent =
    t("dash.assetsSummary", { count: data.asset_count, priced: data.priced_count });
  document.getElementById("generated").textContent =
    t("status.updatedAt", { time: new Date(data.generated_at).toLocaleString() });

  const priced = data.assets.filter((a) => a.has_price && a.value !== null);
  const { slices, colorByAsset } = buildChartSlices(priced, total);

  // ダッシュボードは上位のみのプレビュー（全件・少額トグルは「資産別」ページ）
  const tbody = document.querySelector("#assets-table tbody");
  tbody.innerHTML = "";
  data.assets.slice(0, DASH_TOP).forEach((a) => {
    const pct = a.value && total > 0 ? (Number(a.value) / total) * 100 : null;
    const color = colorByAsset[a.asset] || (a.has_price ? OTHER_COLOR : "transparent");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="clickable-cell" data-action="asset-detail">
        ${assetNameHtml(a.asset, `<span class="swatch" style="background:${color}"></span>`)}
      </td>
      <td class="num">${fmtAmount(a.balance)}</td>
      <td class="num">${a.price ? fmtMoney(a.price, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${a.value ? fmtMoney(a.value, cur) : '<span class="muted">-</span>'}</td>
      <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
      <td><button class="tx-link-btn" data-asset="${escapeHtml(a.asset)}">${t("btn.txHistoryShort")}</button></td>
    `;
    tr.querySelector("[data-action='asset-detail']").addEventListener("click", () =>
      navigateToAssetDetail(a.asset));
    tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      navigateToTransactions({ asset: a.asset });
    });
    tbody.appendChild(tr);
  });

  const moreAssets = document.getElementById("assets-more");
  if (moreAssets) {
    moreAssets.textContent = t("dash.showAllAssets");
    moreAssets.style.display = data.assets.length > DASH_TOP ? "" : "none";
  }

  renderChart(slices, cur, total);
  renderLegend(slices, cur, total);
  renderWarnings(data.warnings);
}

function renderSources(data) {
  const cur = data.currency;
  const tbody = document.querySelector("#sources-table tbody");
  tbody.innerHTML = "";
  data.sources.slice(0, DASH_TOP).forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="clickable-cell" data-action="account-detail">
        ${escapeHtml(s.source)} <span class="row-arrow">›</span>
      </td>
      <td class="num">${s.tx_count}</td>
      <td class="num">${fmtMoney(s.total_value, cur)}</td>
      <td><button class="tx-link-btn" data-account="${escapeHtml(s.source)}">${t("btn.txHistoryShort")}</button></td>
    `;
    tr.querySelector("[data-action='account-detail']").addEventListener("click", () =>
      navigateToAccountDetail(s.source));
    tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      navigateToTransactions({ account: s.source });
    });
    tbody.appendChild(tr);
  });

  const moreSources = document.getElementById("sources-more");
  if (moreSources) {
    moreSources.textContent = t("dash.showAllAccounts", { count: data.sources.length });
    moreSources.style.display = data.sources.length > DASH_TOP ? "" : "none";
  }
}

// ---- チャート ----

// Canvas 内に描画する画像のプリロードキャッシュ。
// crossOrigin は付けない: CoinGecko CDN は CORS ヘッダーを返さないため、
// anonymous 指定だと読み込み自体が失敗する。表示目的の drawImage は
// canvas が汚染されても問題ない（ピクセル読み取りはしないため）。
const _imgCache = new Map();
function _getImg(url) {
  if (_imgCache.has(url)) return _imgCache.get(url);
  const img = new Image();
  img.onload = () => { if (allocChart) allocChart.draw(); };
  img.src = url;
  _imgCache.set(url, img);
  return img;
}

// 円形クリップしてアイコン画像を Canvas に描画するヘルパー。
function _drawCircleIcon(ctx, img, cx, cy, r) {
  if (!img.complete || !img.naturalWidth) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.clip();
  ctx.drawImage(img, cx - r, cy - r, r * 2, r * 2);
  ctx.restore();
}

const centerTextPlugin = {
  id: "centerText",
  afterDraw(chart) {
    const { ctx, chartArea } = chart;
    if (!chartArea) return;
    const cx = (chartArea.left + chartArea.right) / 2;
    const cy = (chartArea.top + chartArea.bottom) / 2;
    const cur = chart.$currency;
    const total = chart.$total || 0;
    const FONT = "-apple-system, 'Noto Sans JP', sans-serif";
    const ICON_R = 14; // アイコン半径 px

    let title, sub, amount, iconUrl;
    const idx = chart.$activeIndex;
    if (idx != null && chart.data.labels[idx] != null) {
      const val = chart.data.datasets[0].data[idx];
      const pct = total > 0 ? ((val / total) * 100).toFixed(1) + "%" : "";
      title = chart.data.labels[idx];
      sub = fmtMoney(val, cur) + (pct ? `  (${pct})` : "");
      const bal = (chart.$balances || [])[idx];
      if (bal != null && bal !== "") amount = fmtAmount(bal) + " " + title;
      iconUrl = _coinIcons[title.toUpperCase()];
    } else {
      title = t("label.total");
      sub = fmtMoney(total, cur);
    }

    const hasIcon = !!iconUrl;
    // アイコンあり: icon → title → sub → amount の4段レイアウト
    // アイコンなし: title → sub (→ amount) の2/3段レイアウト
    const blockH = hasIcon ? (ICON_R * 2 + 4 + 17 + 6 + 19 + (amount ? 18 : 0)) : (17 + 6 + 19 + (amount ? 18 : 0));
    let top = cy - blockH / 2;

    ctx.save();
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const th = chartTheme();

    if (hasIcon) {
      const img = _getImg(iconUrl);
      _drawCircleIcon(ctx, img, cx, top + ICON_R, ICON_R);
      top += ICON_R * 2 + 4;
    }

    ctx.fillStyle = th.tick;
    ctx.font = `600 15px ${FONT}`;
    ctx.fillText(title, cx, top);
    top += 17 + 6;

    ctx.fillStyle = th.tooltipTitle;
    ctx.font = `700 17px ${FONT}`;
    ctx.fillText(sub, cx, top);
    top += 19;

    if (amount) {
      ctx.fillStyle = th.tick;
      ctx.font = `11px ${FONT}`;
      ctx.fillText(amount, cx, top);
    }

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
  allocChart.$balances = slices.map((s) => s.balance);
}

function renderLegend(slices, currency, total) {
  const el = document.getElementById("chart-legend");
  el.innerHTML = "";
  slices.forEach((s, i) => {
    const pct = total > 0 ? (s.value / total) * 100 : 0;
    const row = document.createElement("div");
    row.className = "legend-item";
    const balText = (s.balance != null && s.balance !== "")
      ? `<span class="legend-balance">${fmtAmount(s.balance)} ${escapeHtml(s.label)}</span>`
      : "";
    row.innerHTML = `
      <span class="legend-swatch" style="background:${s.color}"></span>
      ${coinIconHtml(s.label)}
      <span class="legend-name">${escapeHtml(s.label)}</span>
      <span class="legend-pct">${pct.toFixed(1)}%</span>
      <span class="legend-value">${fmtMoney(s.value, currency)}${balText}</span>
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

// ---- 推移グラフ ----

function renderHistoryChart(canvasId, points, currency, existingChart) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return null;
  if (existingChart) existingChart.destroy();

  const emptyEl = canvas.parentElement.querySelector(".history-empty");

  if (!points || points.length < 2) {
    canvas.style.display = "none";
    if (emptyEl) emptyEl.classList.remove("hidden");
    return null;
  }
  canvas.style.display = "";
  if (emptyEl) emptyEl.classList.add("hidden");

  const labels = points.map((p) => p.t);
  const values = points.map((p) => Number(p.value));
  const balances = points.map((p) => (p.balance != null ? p.balance : null));

  const th = chartTheme();
  return new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: "#2f81f7",
        backgroundColor(ctx) {
          const area = ctx.chart.chartArea;
          if (!area) return "rgba(47,129,247,0.15)";
          const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
          g.addColorStop(0, "rgba(47,129,247,0.25)");
          g.addColorStop(1, "rgba(47,129,247,0)");
          return g;
        },
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          ticks: { color: th.tick, font: { size: 11 }, maxTicksLimit: 8, maxRotation: 0 },
          grid: { color: th.grid },
          border: { display: false },
        },
        y: {
          ticks: {
            color: th.tick,
            font: { size: 11 },
            callback(v) { return fmtMoney(v, currency); },
          },
          grid: { color: th.grid },
          border: { display: false },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: th.tooltipBg,
          borderColor: th.tooltipBorder,
          borderWidth: 1,
          titleColor: th.tooltipTitle,
          bodyColor: th.tooltipBody,
          padding: 10,
          callbacks: {
            title: ([item]) => item.label,
            label: (item) => "  " + fmtMoney(item.parsed.y, currency),
            afterLabel: (item) => {
              const bal = balances[item.dataIndex];
              return bal != null ? "  " + fmtAmount(bal) : undefined;
            },
          },
        },
      },
    },
  });
}

function _setRangeActive(tabsId, range) {
  const tabs = document.getElementById(tabsId);
  if (!tabs) return;
  tabs.querySelectorAll(".range-tab").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.range === range));
}

async function _fetchHistAndRender(scope, range, canvasId, loadingId, unpricedId, getRef, setRef) {
  const currency = document.getElementById("currency").value;
  const loading = document.getElementById(loadingId);
  const unpricedEl = document.getElementById(unpricedId);
  if (loading) loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/portfolio-history?scope=${encodeURIComponent(scope)}&range=${range}&currency=${currency}`
    );
    setRef(renderHistoryChart(canvasId, data.points, currency, getRef()));
    if (unpricedEl) {
      if (data.is_partial) {
        unpricedEl.textContent = t("label.historyPartial");
        unpricedEl.classList.remove("hidden");
      } else {
        unpricedEl.classList.add("hidden");
      }
    }
  } catch (e) {
    console.warn("[crypto-summary] portfolio history:", e);
    setRef(renderHistoryChart(canvasId, [], currency, getRef()));
  } finally {
    if (loading) loading.classList.add("hidden");
  }
}

function loadDashHistoryChart(range) {
  _dashHistRange = range || _dashHistRange;
  localStorage.setItem("cs_dash_range", _dashHistRange);
  _setRangeActive("dash-range-tabs", _dashHistRange);
  return _fetchHistAndRender(
    "total", _dashHistRange,
    "history-chart", "history-loading", "history-unpriced",
    () => _histChart, (c) => { _histChart = c; }
  );
}

function loadAcctHistoryChart(name, range) {
  if (name != null) _acctHistName = name;
  if (range != null) _acctHistRange = range;
  if (!_acctHistName) return;
  localStorage.setItem("cs_acct_range", _acctHistRange);
  _setRangeActive("acct-range-tabs", _acctHistRange);
  return _fetchHistAndRender(
    `account:${_acctHistName}`, _acctHistRange,
    "acct-history-chart", "acct-history-loading", "acct-history-unpriced",
    () => _acctHistChart, (c) => { _acctHistChart = c; }
  );
}

function loadAssetHistoryChart(symbol, range) {
  if (symbol != null) _assetHistSymbol = symbol;
  if (range != null) _assetHistRange = range;
  if (!_assetHistSymbol) return;
  localStorage.setItem("cs_asset_range", _assetHistRange);
  _setRangeActive("asset-range-tabs", _assetHistRange);
  return _fetchHistAndRender(
    `asset:${_assetHistSymbol}`, _assetHistRange,
    "asset-history-chart", "asset-history-loading", "asset-history-unpriced",
    () => _assetHistChart, (c) => { _assetHistChart = c; }
  );
}

// ---- 口座別ページ ----

function showAccountsList() {
  document.getElementById("accounts-list-view").classList.remove("hidden");
  document.getElementById("account-detail-view").classList.add("hidden");
}

function showAccountDetail(name) {
  _currentAccountName = name;
  document.getElementById("accounts-list-view").classList.add("hidden");
  const detail = document.getElementById("account-detail-view");
  detail.classList.remove("hidden");
  document.getElementById("account-settings-panel").classList.add("hidden");
  document.getElementById("settings-result").classList.add("hidden");
  document.getElementById("account-detail-name").textContent = name;
  document.getElementById("account-tx-link").onclick = () =>
    navigateToTransactions({ account: name });
  // 別口座へ移動したらレンジをリセット
  if (_acctHistName !== name) _acctHistRange = "90d";
  loadAccountDetail(name);
  loadAcctHistoryChart(name, _acctHistRange);
}

async function loadAccountsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#accounts-table tbody");
  tbody.innerHTML = `<tr><td colspan="5" class="loading">${t("label.loading")}</td></tr>`;
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
        <td><button class="tx-link-btn">${t("btn.txHistoryShort")}</button></td>
      `;
      tr.querySelector("[data-action='account-detail']").addEventListener("click", () =>
        navigateToAccountDetail(s.source));
      tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        navigateToTransactions({ account: s.source });
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadAccountDetail(name) {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#account-assets-table tbody");
  const loading = document.getElementById("account-detail-loading");
  const walletInfo = document.getElementById("account-wallet-info");
  tbody.innerHTML = "";
  walletInfo.innerHTML = "";
  walletInfo.classList.add("hidden");
  loading.classList.remove("hidden");
  try {
    const data = await fetchJSON(
      `/api/account-assets?account=${encodeURIComponent(name)}&currency=${currency}`
    );
    loading.classList.add("hidden");
    if (data.wallets && data.wallets.length > 0) {
      const chips = data.wallets.map((w) => {
        const short = w.address.length > 20
          ? `${w.address.slice(0, 10)}…${w.address.slice(-8)}` : w.address;
        return `<span class="wallet-address-chip" title="${escapeHtml(w.address)}">
          <span class="wallet-chain-label">${escapeHtml(w.chain_label)}</span>
          <code class="wallet-addr-text">${escapeHtml(short)}</code>
          <button class="wallet-copy-btn" data-address="${escapeHtml(w.address)}" title="copy">⧉</button>
        </span>`;
      }).join("");
      walletInfo.innerHTML = `<div class="wallet-info-row"><span class="wallet-info-label">${t("label.walletAddress")}</span>${chips}</div>`;
      walletInfo.classList.remove("hidden");
      walletInfo.querySelectorAll(".wallet-copy-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          navigator.clipboard.writeText(btn.dataset.address).then(() => {
            const orig = btn.textContent;
            btn.textContent = "✓";
            setTimeout(() => { btn.textContent = orig; }, 1500);
          });
        });
      });
    }
    data.assets.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${assetNameHtml(a.asset)}</td>
        <td class="num">${fmtAmount(a.balance)}</td>
        <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
        <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
        <td><button class="tx-link-btn">${t("btn.txHistoryShort")}</button></td>
      `;
      tr.querySelector(".tx-link-btn").addEventListener("click", () =>
        navigateToTransactions({ account: name, asset: a.asset }));
      tbody.appendChild(tr);
    });
    if (data.assets.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("label.noBalance")}</td></tr>`;
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- 口座設定パネル ----

let _currentAccountName = null;

document.getElementById("account-settings-btn").addEventListener("click", () => {
  const panel = document.getElementById("account-settings-panel");
  if (panel.classList.contains("hidden")) {
    openAccountSettings(_currentAccountName);
  } else {
    panel.classList.add("hidden");
  }
});

document.getElementById("settings-cancel").addEventListener("click", () => {
  document.getElementById("account-settings-panel").classList.add("hidden");
});

async function openAccountSettings(accountName) {
  const panel = document.getElementById("account-settings-panel");
  const result = document.getElementById("settings-result");
  result.classList.add("hidden");
  panel.classList.remove("hidden");

  document.getElementById("settings-display-name").value = accountName;

  try {
    const [groupData, sourcesData] = await Promise.all([
      fetchJSON("/api/account-groups"),
      fetchJSON("/api/sources?currency=USD"),
    ]);
    const thisAccount = sourcesData.sources.find((s) => s.source === accountName);
    const assignedIds = thisAccount ? thisAccount.source_ids : [];
    const otherIds = groupData.all_source_ids.filter((s) => !assignedIds.includes(s));
    renderSettingsSourceIds(assignedIds, otherIds);
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.settingsLoadFail") + ": " + e.message;
    result.classList.remove("hidden");
  }
}

function renderSettingsSourceIds(assignedIds, otherIds) {
  const assignedWrap = document.getElementById("settings-source-ids");
  assignedWrap.innerHTML = "";
  assignedIds.forEach((sid) => assignedWrap.appendChild(makeSourceChip(sid, true)));
  if (assignedIds.length === 0) {
    assignedWrap.innerHTML = `<span class="muted" style="font-size:12px">${t("label.none")}</span>`;
  }

  const unassignedWrap = document.getElementById("settings-unassigned-ids");
  unassignedWrap.innerHTML = "";
  if (otherIds.length === 0) {
    unassignedWrap.innerHTML = `<span class="muted" style="font-size:12px">${t("label.none")}</span>`;
  } else {
    otherIds.forEach((sid) => unassignedWrap.appendChild(makeSourceChip(sid, false, true)));
  }
}

function makeSourceChip(sid, checked, isUnassigned = false) {
  const label = document.createElement("label");
  label.className = "source-id-chip" + (isUnassigned ? " unassigned" : "");
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.value = sid;
  cb.checked = checked;
  cb.dataset.sid = sid;
  label.appendChild(cb);
  label.appendChild(document.createTextNode(sid));
  return label;
}

document.getElementById("settings-save").addEventListener("click", async () => {
  const result = document.getElementById("settings-result");
  result.classList.add("hidden");

  const newName = document.getElementById("settings-display-name").value.trim();
  if (!newName) {
    result.className = "settings-result err";
    result.textContent = t("status.nameRequired");
    result.classList.remove("hidden");
    return;
  }

  const checkedIds = [
    ...document.querySelectorAll("#settings-source-ids input[type=checkbox]:checked"),
    ...document.querySelectorAll("#settings-unassigned-ids input[type=checkbox]:checked"),
  ].map((cb) => cb.dataset.sid);

  try {
    const data = await fetchJSON("/api/account-groups");
    const groups = {};
    for (const [name, ids] of Object.entries(data.groups)) {
      if (name === _currentAccountName) continue;
      const remaining = ids.filter((id) => !checkedIds.includes(id));
      if (remaining.length > 0) groups[name] = remaining;
    }
    if (checkedIds.length > 0) {
      groups[newName] = checkedIds;
    }

    const resp = await fetch("/api/account-groups", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ groups }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    result.className = "settings-result ok";
    result.textContent = t("status.settingsSaved");
    result.classList.remove("hidden");

    if (newName !== _currentAccountName) {
      _currentAccountName = newName;
      document.getElementById("account-detail-name").textContent = newName;
      document.getElementById("account-tx-link").onclick = () =>
        navigateToTransactions({ account: newName });
    }

    loadAccountsPage();
    // 取引履歴フィルタ選択肢も更新
    _txAccountsLoaded = false;
    _rebuildAccountFilter();

  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.settingsSaveFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

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
  document.getElementById("asset-tx-link").onclick = () =>
    navigateToTransactions({ asset: symbol });
  // 別資産へ移動したらレンジをリセット
  if (_assetHistSymbol !== symbol) _assetHistRange = "90d";
  loadAssetDetail(symbol);
  loadAssetHistoryChart(symbol, _assetHistRange);
}

async function loadAssetsPage() {
  const currency = document.getElementById("currency").value;
  const tbody = document.querySelector("#all-assets-table tbody");
  tbody.innerHTML = `<tr><td colspan="6" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON(`/api/summary?currency=${currency}`);
    lastAssetsData = data;
    renderAllAssets(data, currency);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// 資産別ページの全件リスト（少額トグル・価格未対応の注記つき）を描画する。
function renderAllAssets(data, currency) {
  const total = Number(data.total_value) || 0;
  const tbody = document.querySelector("#all-assets-table tbody");
  tbody.innerHTML = "";
  let hiddenCount = 0;
  data.assets.forEach((a) => {
    const small = isSmall(a, currency);
    if (small) hiddenCount++;
    if (small && !showSmall) return;

    const pct = a.value && total > 0 ? (Number(a.value) / total) * 100 : null;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="clickable-cell" data-action="asset-detail">
        ${assetNameHtml(a.asset)} <span class="row-arrow">›</span>
      </td>
      <td class="num">${fmtAmount(a.balance)}</td>
      <td class="num">${a.price ? fmtMoney(a.price, currency) : '<span class="muted">-</span>'}</td>
      <td class="num">${a.value ? fmtMoney(a.value, currency) : '<span class="muted">-</span>'}</td>
      <td class="num">${pct !== null ? pct.toFixed(1) + "%" : '<span class="muted">-</span>'}</td>
      <td><button class="tx-link-btn">${t("btn.txHistoryShort")}</button></td>
    `;
    tr.querySelector("[data-action='asset-detail']").addEventListener("click", () =>
      navigateToAssetDetail(a.asset));
    tr.querySelector(".tx-link-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      navigateToTransactions({ asset: a.asset });
    });
    tbody.appendChild(tr);
  });
  if (data.assets.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${t("label.noData")}</td></tr>`;
  }

  const toggleBtn = document.getElementById("toggle-small");
  if (toggleBtn) {
    if (hiddenCount > 0 || showSmall) {
      toggleBtn.style.display = "";
      toggleBtn.textContent = showSmall
        ? t("label.hideSmall")
        : t("label.showSmall", { count: hiddenCount });
    } else {
      toggleBtn.style.display = "none";
    }
  }

  const unpricedEl = document.getElementById("unpriced");
  if (unpricedEl) {
    if (data.unpriced && data.unpriced.length) {
      unpricedEl.classList.remove("hidden");
      unpricedEl.textContent = t("label.unpricedAssets") + data.unpriced.join(", ");
    } else {
      unpricedEl.classList.add("hidden");
    }
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
        <td><button class="tx-link-btn">${t("btn.txHistoryShort")}</button></td>
      `;
      tr.querySelector(".tx-link-btn").addEventListener("click", () =>
        navigateToTransactions({ account: a.account, asset: symbol }));
      tbody.appendChild(tr);
    });
    if (data.accounts.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("label.noAccountsHere")}</td></tr>`;
    }
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// ---- 取引履歴ページ ----

let _txAccountsLoaded = false;

async function _rebuildAccountFilter() {
  _txAccountsLoaded = false;
  const accSel = document.getElementById("tx-filter-account");
  [...accSel.options].forEach((o) => { if (o.value) o.remove(); });
  await _ensureAccountOptions();
}

async function _ensureAccountOptions() {
  if (_txAccountsLoaded) return;
  try {
    const sources = await fetchJSON("/api/sources?currency=USD");
    const accSel = document.getElementById("tx-filter-account");
    // clear existing non-empty options to avoid duplication
    [...accSel.options].forEach((o) => { if (o.value) o.remove(); });
    sources.sources.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.source;
      opt.textContent = s.source;
      accSel.appendChild(opt);
    });
    _txAccountsLoaded = true;
  } catch (e) { console.warn("[crypto-summary] dropdown/options load failed:", e); }
}

async function _updateAssetDropdown(account) {
  const assetSel = document.getElementById("tx-filter-asset");
  const prevValue = assetSel.value;
  // clear all asset options except the first ("すべての資産")
  [...assetSel.options].forEach((o) => { if (o.value) o.remove(); });

  try {
    let assets;
    if (account) {
      const data = await fetchJSON(`/api/account-assets?account=${encodeURIComponent(account)}&currency=USD`);
      assets = data.assets.map((a) => a.asset).sort((x, y) => x.localeCompare(y));
    } else {
      const data = await fetchJSON("/api/summary?currency=USD");
      assets = data.assets.map((a) => a.asset).sort((x, y) => x.localeCompare(y));
    }
    assets.forEach((sym) => {
      const opt = document.createElement("option");
      opt.value = sym;
      opt.textContent = sym;
      assetSel.appendChild(opt);
    });
    // restore previous selection if still available
    if ([...assetSel.options].some((o) => o.value === prevValue)) {
      assetSel.value = prevValue;
    }
  } catch (e) { console.warn("[crypto-summary] dropdown/options load failed:", e); }
}

async function loadTransactionsPage(account, asset, since, until, page = 1) {
  await _ensureAccountOptions();
  ensureExportFormats();
  await initTaxExportPanel();

  // 口座で絞り込まれている場合は確定申告用エクスポートの口座も合わせる
  const taxAcct = document.getElementById("tax-account");
  if (taxAcct) {
    const want = account || "";
    if ([...taxAcct.options].some((o) => o.value === want)) taxAcct.value = want;
  }

  // フィルタUIを同期
  const selAccount = document.getElementById("tx-filter-account");
  const selAsset = document.getElementById("tx-filter-asset");
  const selSince = document.getElementById("tx-filter-since");
  const selUntil = document.getElementById("tx-filter-until");

  selAccount.value = account || "";
  selSince.value = since || "";
  selUntil.value = until || "";

  // 資産ドロップダウンを口座に合わせて更新してから選択値を設定
  await _updateAssetDropdown(account || null);
  if (asset) {
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
  if (account) parts.push(t("filter.account") + `<strong>${escapeHtml(account)}</strong>`);
  if (asset) parts.push(t("filter.asset") + `<strong>${escapeHtml(asset)}</strong>`);
  if (since) parts.push(t("filter.from") + `<strong>${since}</strong>`);
  if (until) parts.push(t("filter.to") + `<strong>${until}</strong>`);
  if (parts.length) {
    banner.innerHTML = t("filter.active") + parts.join(" ／ ");
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
    if (since) url += `&since=${encodeURIComponent(since)}`;
    if (until) url += `&until=${encodeURIComponent(until)}`;

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

        // 取引後残高 — 複数資産対応
        const rb = tx.running_balances || {};
        let balHtml = "";
        const rbEntries = Object.entries(rb);
        if (rbEntries.length === 0) {
          balHtml = '<span class="muted">-</span>';
        } else {
          balHtml = '<div class="bal-cell">';
          rbEntries.forEach(([sym, bal]) => {
            balHtml += `<div class="bal-entry">
              <span class="bal-asset">${escapeHtml(sym)}</span>
              <span class="bal-vals">${fmtAmount(bal.global)}<span class="bal-acct"> (${fmtAmount(bal.account)})</span></span>
            </div>`;
          });
          balHtml += "</div>";
        }

        const isManual = tx.id.startsWith("manual:");
        const delBtn = `<button class="delete-btn" title="${t("btn.delete")}" data-txid="${escapeHtml(tx.id)}" data-txdesc="${escapeHtml(fmtDate(tx.timestamp) + " " + (tx.type_ja || tx.type))}">✕</button>`;

        tr.innerHTML = `
          <td style="white-space:nowrap">${fmtDate(tx.timestamp)}</td>
          <td>${escapeHtml(tx.account)}</td>
          <td><span class="tx-type tx-type-${escapeHtml(tx.type)}">${escapeHtml(_txTypeLabel(tx))}</span></td>
          <td>${recv}</td>
          <td>${sent}</td>
          <td>${fee}</td>
          <td class="num">${balHtml}</td>
          <td style="white-space:nowrap">${tx.label ? '<span class="muted" style="font-size:12px">' + escapeHtml(tx.label) + '</span>' : ""}${hash}${delBtn}</td>
        `;

        tr.querySelector(".delete-btn").addEventListener("click", (e) => {
          e.stopPropagation();
          const btn = e.currentTarget;
          showDeleteDialog(btn.dataset.txid, btn.dataset.txdesc, () => {
            loadTransactionsPage(account, asset, since, until, page);
          });
        });

        tbody.appendChild(tr);
      });
    }

    renderTxPagination(data, account, asset, since, until);
  } catch (e) {
    loading.classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="8" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
    document.getElementById("tx-pagination").innerHTML = "";
  }
}

function renderTxPagination(data, account, asset, since, until) {
  const el = document.getElementById("tx-pagination");
  el.innerHTML = "";
  if (data.total_pages <= 1) {
    const info = document.createElement("span");
    info.className = "page-info";
    info.textContent = t("filter.count", { count: data.total.toLocaleString() });
    el.appendChild(info);
    return;
  }

  const cur = data.page;
  const total = data.total_pages;

  if (cur > 1) {
    const btn = document.createElement("button");
    btn.textContent = "‹";
    btn.addEventListener("click", () => navigateToTransactions({ account, asset, since, until, page: cur - 1 }));
    el.appendChild(btn);
  }

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
      btn.addEventListener("click", () => navigateToTransactions({ account, asset, since, until, page: p }));
      el.appendChild(btn);
    }
  });

  if (cur < total) {
    const btn = document.createElement("button");
    btn.textContent = "›";
    btn.addEventListener("click", () => navigateToTransactions({ account, asset, since, until, page: cur + 1 }));
    el.appendChild(btn);
  }

  const info = document.createElement("span");
  info.className = "page-info";
  info.textContent = t("filter.count", { count: data.total.toLocaleString() });
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

document.getElementById("tx-filter-account").addEventListener("change", async (e) => {
  const account = e.target.value || null;
  const since = document.getElementById("tx-filter-since").value || null;
  const until = document.getElementById("tx-filter-until").value || null;
  // 口座を変えたら資産ドロップダウンを更新してから遷移
  await _updateAssetDropdown(account);
  navigateToTransactions({ account, asset: null, since, until, page: 1 });
});

document.getElementById("tx-filter-asset").addEventListener("change", (e) => {
  const asset = e.target.value || null;
  const account = document.getElementById("tx-filter-account").value || null;
  const since = document.getElementById("tx-filter-since").value || null;
  const until = document.getElementById("tx-filter-until").value || null;
  navigateToTransactions({ account, asset, since, until, page: 1 });
});

document.getElementById("tx-filter-since").addEventListener("change", (e) => {
  const since = e.target.value || null;
  const account = document.getElementById("tx-filter-account").value || null;
  const asset = document.getElementById("tx-filter-asset").value || null;
  const until = document.getElementById("tx-filter-until").value || null;
  navigateToTransactions({ account, asset, since, until, page: 1 });
});

document.getElementById("tx-filter-until").addEventListener("change", (e) => {
  const until = e.target.value || null;
  const account = document.getElementById("tx-filter-account").value || null;
  const asset = document.getElementById("tx-filter-asset").value || null;
  const since = document.getElementById("tx-filter-since").value || null;
  navigateToTransactions({ account, asset, since, until, page: 1 });
});

document.getElementById("tx-filter-clear").addEventListener("click", () => {
  document.getElementById("tx-filter-since").value = "";
  document.getElementById("tx-filter-until").value = "";
  navigateToTransactions({});
});

// ---- CSVエクスポート ----

let _exportFormatsLoaded = false;

async function ensureExportFormats() {
  if (_exportFormatsLoaded) return;
  const sel = document.getElementById("tx-export-format");
  try {
    const data = await fetchJSON("/api/export/formats");
    sel.innerHTML = "";
    data.formats.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.value;
      opt.textContent = f.label;
      sel.appendChild(opt);
    });
    _exportFormatsLoaded = true;
  } catch (e) { console.warn("[crypto-summary] dropdown/options load failed:", e); }
}

document.getElementById("tx-export-btn").addEventListener("click", async () => {
  const result = document.getElementById("tx-export-result");
  result.classList.add("hidden");

  const format = document.getElementById("tx-export-format").value;
  const account = document.getElementById("tx-filter-account").value || null;
  const since = document.getElementById("tx-filter-since").value || null;
  const until = document.getElementById("tx-filter-until").value || null;

  let url = `/api/export?format=${encodeURIComponent(format)}`;
  if (account) url += `&account=${encodeURIComponent(account)}`;
  if (since) url += `&since=${encodeURIComponent(since)}`;
  if (until) url += `&until=${encodeURIComponent(until)}`;

  result.className = "settings-result ok";
  result.textContent = t("status.exportRunning");
  result.classList.remove("hidden");

  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    // ファイル名を Content-Disposition から取得
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const filename = m ? m[1] : `${format}.csv`;

    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);

    result.className = "settings-result ok";
    result.textContent = t("status.exportDoneFile", { filename });
    result.classList.remove("hidden");
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.exportFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

// ---- 確定申告用エクスポートパネル ----

let _taxYear = null;
let _taxPanelLoaded = false;

async function initTaxExportPanel() {
  if (_taxPanelLoaded) return;
  _taxPanelLoaded = true;

  // 年度ボタン（今年 + 過去3年）
  const btns = document.getElementById("tax-year-btns");
  if (btns) {
    const cur = new Date().getFullYear();
    for (let y = cur; y >= cur - 3; y--) {
      const btn = document.createElement("button");
      btn.className = "tax-year-btn";
      btn.textContent = `${y}`;
      btn.dataset.year = y;
      btn.addEventListener("click", () => {
        _taxYear = y;
        btns.querySelectorAll(".tax-year-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
      });
      btns.appendChild(btn);
    }
    // デフォルト: 前年を選択
    _taxYear = cur - 1;
    btns.querySelector(`[data-year="${cur - 1}"]`)?.classList.add("active");
  }

  // 口座リスト（API から取得して複製）
  await _ensureAccountOptions();
  const taxAcct = document.getElementById("tax-account");
  const filterAcct = document.getElementById("tx-filter-account");
  if (taxAcct && filterAcct) {
    // すべての口座 option を残しつつ各口座を追加
    [...filterAcct.options].forEach((opt) => {
      if (!opt.value) return; // 「すべての口座」は既にある
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.textContent;
      taxAcct.appendChild(o);
    });
  }

  // エクスポート形式
  await ensureExportFormats();
  const taxFmt = document.getElementById("tax-format");
  const mainFmt = document.getElementById("tx-export-format");
  if (taxFmt && mainFmt) {
    taxFmt.innerHTML = mainFmt.innerHTML;
  }
}

document.getElementById("tax-export-btn").addEventListener("click", async () => {
  if (!_taxYear) return;
  const result = document.getElementById("tax-export-result");
  result.classList.add("hidden");

  const format = document.getElementById("tax-format").value;
  const account = document.getElementById("tax-account").value || null;
  const since = `${_taxYear}-01-01`;
  const until = `${_taxYear}-12-31`;

  let url = `/api/export?format=${encodeURIComponent(format)}&since=${since}&until=${until}`;
  if (account) url += `&account=${encodeURIComponent(account)}`;

  result.className = "settings-result ok";
  result.textContent = t("status.exportRunning");
  result.classList.remove("hidden");

  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const filename = m ? m[1] : `${format}.csv`;
    const blob = await resp.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);
    result.className = "settings-result ok";
    result.textContent = t("status.exportDoneFile", { filename });
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.exportFail", { error: e.message });
  }
  result.classList.remove("hidden");
});

// ---- 手動追加フォーム ----

document.getElementById("tx-add-btn").addEventListener("click", async () => {
  const form = document.getElementById("tx-add-form");
  const isHidden = form.classList.contains("hidden");
  if (isHidden) {
    // 口座セレクトを最新化
    const accSel = document.getElementById("manual-account");
    accSel.innerHTML = "";
    try {
      const data = await fetchJSON("/api/sources?currency=USD");
      data.sources.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.source;
        opt.textContent = s.source;
        accSel.appendChild(opt);
      });
    } catch (e) { console.warn("[crypto-summary] dropdown/options load failed:", e); }
    // デフォルト日時を今にセット
    const now = new Date();
    const local = new Date(now - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    document.getElementById("manual-timestamp").value = local;
    document.getElementById("manual-result").classList.add("hidden");
  }
  form.classList.toggle("hidden");
});

document.getElementById("manual-cancel").addEventListener("click", () => {
  document.getElementById("tx-add-form").classList.add("hidden");
});

document.getElementById("manual-save").addEventListener("click", async () => {
  const result = document.getElementById("manual-result");
  result.classList.add("hidden");

  const account = document.getElementById("manual-account").value;
  const timestamp = document.getElementById("manual-timestamp").value;
  const type = document.getElementById("manual-type").value;
  const recvAsset = document.getElementById("manual-recv-asset").value.trim().toUpperCase() || null;
  const recvAmount = document.getElementById("manual-recv-amount").value || null;
  const sentAsset = document.getElementById("manual-sent-asset").value.trim().toUpperCase() || null;
  const sentAmount = document.getElementById("manual-sent-amount").value || null;
  const feeAsset = document.getElementById("manual-fee-asset").value.trim().toUpperCase() || null;
  const feeAmount = document.getElementById("manual-fee-amount").value || null;
  const label = document.getElementById("manual-label").value.trim() || null;

  if (!account || !timestamp) {
    result.className = "settings-result err";
    result.textContent = t("status.manualRequired");
    result.classList.remove("hidden");
    return;
  }

  try {
    const resp = await fetch("/api/transactions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account,
        timestamp,
        type,
        received_asset: recvAsset,
        received_amount: recvAmount,
        sent_asset: sentAsset,
        sent_amount: sentAmount,
        fee_asset: feeAsset,
        fee_amount: feeAmount,
        label,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    result.className = "settings-result ok";
    result.textContent = t("status.addDone");
    result.classList.remove("hidden");
    // フォームをリセット
    ["manual-recv-asset", "manual-recv-amount", "manual-sent-asset", "manual-sent-amount",
      "manual-fee-asset", "manual-fee-amount", "manual-label"].forEach((id) => {
      document.getElementById(id).value = "";
    });
    // 現在のフィルタで再ロード
    const account2 = document.getElementById("tx-filter-account").value || null;
    const asset2 = document.getElementById("tx-filter-asset").value || null;
    const since2 = document.getElementById("tx-filter-since").value || null;
    const until2 = document.getElementById("tx-filter-until").value || null;
    loadTransactionsPage(account2, asset2, since2, until2, 1);
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.addFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

// ---- 削除ダイアログ（取引1件 / CSVバッチ / 口座全消去 で共用） ----

let _deleteCallback = null;    // { txId, onSuccess }  — 単一取引削除
let _batchDeleteId = null;     // string               — CSVバッチ削除
let _accountClearTarget = null; // string (表示名)      — 口座全消去
let _apiDeleteSourceId = null; // string               — API口座登録削除
let _walletDeleteSourceId = null; // string            — ウォレット登録削除

function _clearDialogState() {
  _deleteCallback = null;
  _batchDeleteId = null;
  _accountClearTarget = null;
  _apiDeleteSourceId = null;
  _walletDeleteSourceId = null;
}

// タイトル・本文をセットしてダイアログを表示する（用途に応じてタイトルを変える）。
function _openDeleteDialog(title, msg) {
  document.getElementById("delete-dialog-title").textContent = title;
  document.getElementById("delete-dialog-msg").textContent = msg;
  document.getElementById("delete-dialog").classList.remove("hidden");
}

function showDeleteDialog(txId, desc, onSuccess) {
  _clearDialogState();
  _deleteCallback = { txId, onSuccess };
  _openDeleteDialog(t("status.txDelConfirmTitle"),
    t("status.txDelConfirmBody", { desc }));
}

function showBatchDeleteDialog(batchId, desc) {
  _clearDialogState();
  _batchDeleteId = batchId;
  _openDeleteDialog(t("status.csvDelConfirmTitle"),
    t("status.csvDelConfirmBody", { desc }));
}

document.getElementById("delete-confirm").addEventListener("click", async () => {
  document.getElementById("delete-dialog").classList.add("hidden");

  // API口座登録削除
  if (_apiDeleteSourceId) {
    const sourceId = _apiDeleteSourceId;
    _clearDialogState();
    try {
      const resp = await fetch(`/api/account-apis/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      loadApiAccountsTable();
      const result = document.getElementById("import-api-result");
      result.className = "settings-result ok";
      result.textContent = t("status.apiDelDone", { id: sourceId });
      result.classList.remove("hidden");
    } catch (e) {
      alert(t("status.apiDelFail", { error: e.message }));
    }
    return;
  }

  // ウォレット登録削除
  if (_walletDeleteSourceId) {
    const sourceId = _walletDeleteSourceId;
    _clearDialogState();
    try {
      const resp = await fetch(`/api/wallets/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }
      loadWalletsTable();
      const result = document.getElementById("import-wallet-result");
      result.className = "settings-result ok";
      result.textContent = t("status.walletDelDone", { id: sourceId });
      result.classList.remove("hidden");
    } catch (e) {
      alert(t("status.walletDelFail", { error: e.message }));
    }
    return;
  }

  // 口座全消去
  if (_accountClearTarget) {
    const account = _accountClearTarget;
    _clearDialogState();
    try {
      const resp = await fetch(`/api/sources/${encodeURIComponent(account)}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      loadImportAccountsTable();
      loadImportBatches();
      _txAccountsLoaded = false;
      const result = document.getElementById("import-csv-result");
      result.className = "settings-result ok";
      result.textContent = t("status.txDelDone", { account, count: d.deleted });
      result.classList.remove("hidden");
    } catch (e) {
      alert(t("status.walletDelFail", { error: e.message }));
    }
    return;
  }

  // CSVバッチ削除
  if (_batchDeleteId) {
    const batchId = _batchDeleteId;
    _clearDialogState();
    try {
      const resp = await fetch(`/api/import/batches/${encodeURIComponent(batchId)}`, { method: "DELETE" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      loadImportBatches();
      loadImportAccountsTable();
      _txAccountsLoaded = false;
      const result = document.getElementById("import-csv-result");
      result.className = "settings-result ok";
      result.textContent = t("status.deleteCount", { count: d.deleted });
      result.classList.remove("hidden");
    } catch (e) {
      alert(t("status.walletDelFail", { error: e.message }));
    }
    return;
  }

  // 単一取引削除
  if (!_deleteCallback) return;
  const { txId, onSuccess } = _deleteCallback;
  _clearDialogState();
  try {
    const resp = await fetch(`/api/transactions/${encodeURIComponent(txId)}`, { method: "DELETE" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    if (onSuccess) onSuccess();
  } catch (e) {
    alert(t("status.walletDelFail", { error: e.message }));
  }
});

document.getElementById("delete-cancel").addEventListener("click", () => {
  _clearDialogState();
  document.getElementById("delete-dialog").classList.add("hidden");
});

// ダイアログ外クリックで閉じる
document.getElementById("delete-dialog").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) {
    _clearDialogState();
    e.currentTarget.classList.add("hidden");
  }
});

// Esc キーでダイアログを閉じる
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const dialog = document.getElementById("delete-dialog");
  if (!dialog.classList.contains("hidden")) {
    _clearDialogState();
    dialog.classList.add("hidden");
  }
});

// ---- インポートページ ----

let _importTabsReady = false;
let _importExchangesLoaded = false;

async function loadImportPage() {
  setupImportTabs();
  ensureImportExchanges();
  loadImportAccountsTable();
  loadImportBatches();
  loadApiAccountsTable();
  loadWalletsTable();
  loadSystemKeys();
}

// システム設定（管理者のみ）: スキャン用キー・マスター鍵の状態を読み込んで表示する。
async function loadSystemKeys() {
  const panel = document.getElementById("system-keys-panel");
  if (!panel) return;
  if (!window._isAdmin) {
    panel.classList.add("hidden");
    return;
  }
  try {
    const data = await fetchJSON("/api/system-keys");
    panel.classList.remove("hidden");
    const p = data.providers || {};
    const setKey = (id, info) => {
      const el = document.getElementById(id);
      if (!el) return;
      info = info || {};
      if (info.stored) {
        el.textContent = t("label.keySet");
        el.className = "settings-hint key-set";
      } else if (info.env) {
        el.textContent = t("system.fromEnv");
        el.className = "settings-hint key-set";
      } else {
        el.textContent = t("label.keyNotSet");
        el.className = "settings-hint key-unset";
      }
    };
    setKey("system-etherscan-status", p.etherscan);
    setKey("system-helius-status", p.helius);
    const cs = document.getElementById("system-cs-secret-status");
    if (cs) {
      cs.textContent = data.cs_secret_key ? t("label.keySet") : t("label.keyNotSet");
      cs.className = "settings-hint " + (data.cs_secret_key ? "key-set" : "key-unset");
    }
  } catch (e) {
    // 管理者でない場合は 403。パネルは隠したままにする。
    panel.classList.add("hidden");
  }
}


function setupImportTabs() {
  if (_importTabsReady) return;
  _importTabsReady = true;
  document.querySelectorAll(".import-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll(".import-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".import-tab-content").forEach((c) => c.classList.add("hidden"));
      const content = document.getElementById(`import-tab-${tab}`);
      if (content) content.classList.remove("hidden");
    });
  });
}

async function ensureImportExchanges() {
  if (_importExchangesLoaded) return;
  const sel = document.getElementById("import-csv-exchange");
  try {
    const data = await fetchJSON("/api/import/exchanges");
    sel.innerHTML = "";
    data.exchanges.forEach((ex) => {
      const opt = document.createElement("option");
      opt.value = ex.value;
      opt.textContent = ex.label;
      sel.appendChild(opt);
    });
    _importExchangesLoaded = true;
  } catch (e) { console.warn("[crypto-summary] dropdown/options load failed:", e); }
}

async function loadImportAccountsTable() {
  const tbody = document.querySelector("#import-accounts-table tbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="4" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/sources?currency=USD");
    tbody.innerHTML = "";
    if (data.sources.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("label.noAccountsHere")}</td></tr>`;
      return;
    }
    data.sources.forEach((s) => {
      const tr = document.createElement("tr");
      const firstId = (s.source_ids && s.source_ids.length) ? s.source_ids[0] : s.source;
      tr.innerHTML = `
        <td>${escapeHtml(s.source)}</td>
        <td><span class="muted" style="font-size:12px;font-family:monospace">${escapeHtml(s.source_ids.join(", "))}</span></td>
        <td class="num">${s.tx_count}</td>
        <td style="white-space:nowrap;display:flex;gap:6px;align-items:center">
          <button class="tx-link-btn btn-csv-import">${t("btn.csvAppend")}</button>
          <button class="tx-link-btn btn-clear-account" style="border-color:#5a2a2a">${t("btn.clearAll")}</button>
        </td>
      `;
      tr.querySelector(".btn-csv-import").addEventListener("click", () => {
        document.querySelector(".import-tab[data-tab='csv']").click();
        document.getElementById("import-csv-account").value = firstId;
        document.getElementById("import-csv-account").scrollIntoView({ behavior: "smooth" });
      });
      tr.querySelector(".btn-clear-account").addEventListener("click", () => {
        _clearDialogState();
        _accountClearTarget = s.source;
        _openDeleteDialog(t("status.accountClearConfirmTitle"),
          t("status.accountClearConfirmBody", { account: s.source, ids: s.source_ids.join(", "), count: s.tx_count }));
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadImportBatches() {
  const tbody = document.querySelector("#import-batches-table tbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="6" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/import/batches");
    tbody.innerHTML = "";
    if (!data.batches || data.batches.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted">${t("label.noCsvHistory")}</td></tr>`;
      return;
    }
    data.batches.forEach((b) => {
      const tr = document.createElement("tr");
      // existing_count が tx_count と異なる場合は残存件数を併記
      const countLabel = b.existing_count === b.tx_count
        ? `${b.tx_count}`
        : `${b.existing_count} / ${b.tx_count}`;
      tr.innerHTML = `
        <td style="white-space:nowrap">${fmtDate(b.imported_at + "Z")}</td>
        <td>${escapeHtml(b.account)}</td>
        <td>${escapeHtml(b.exchange_label)}</td>
        <td><span class="muted" style="font-size:12px">${escapeHtml(b.filename || "-")}</span></td>
        <td class="num">${countLabel}</td>
        <td><button class="tx-link-btn" style="border-color:#5a2a2a">${t("btn.deleteByFile")}</button></td>
      `;
      tr.querySelector(".tx-link-btn").addEventListener("click", () => {
        const desc = `${b.exchange_label} / ${b.filename || "-"}（${t("filter.count", { count: b.existing_count })}）`;
        showBatchDeleteDialog(b.id, desc);
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadApiAccountsTable() {
  const tbody = document.querySelector("#import-api-accounts-table tbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/account-apis");
    tbody.innerHTML = "";
    if (!data.accounts || data.accounts.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("label.noApiAccounts")}</td></tr>`;
      return;
    }
    data.accounts.forEach((a) => {
      const tr = document.createElement("tr");
      const registeredAt = a.created_at ? fmtDate(a.created_at) : "-";
      tr.innerHTML = `
        <td><code style="font-family:monospace">${escapeHtml(a.source_id)}</code></td>
        <td>${escapeHtml(a.exchange_label || a.exchange)}</td>
        <td>${escapeHtml(a.category)}</td>
        <td style="white-space:nowrap">${escapeHtml(registeredAt)}</td>
        <td style="white-space:nowrap;display:flex;gap:6px;align-items:center">
          <button class="tx-link-btn btn-api-sync">${t("btn.sync")}</button>
          <button class="tx-link-btn btn-api-delete" style="border-color:#5a2a2a">${t("btn.delete")}</button>
        </td>
      `;
      tr.querySelector(".btn-api-sync").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = t("btn.syncing");
        const result = document.getElementById("import-api-result");
        result.className = "settings-result ok";
        result.textContent = t("status.walletScanning", { id: a.source_id });
        result.classList.remove("hidden");
        try {
          const resp = await fetch(`/api/account-apis/${encodeURIComponent(a.source_id)}/sync`, {
            method: "POST",
          });
          const d = await resp.json();
          if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);
          result.className = "settings-result ok";
          result.textContent = t("status.apiSyncDone", { fetched: d.fetched, imported: d.imported, id: a.source_id });
          loadImportAccountsTable();
          _txAccountsLoaded = false;
        } catch (err) {
          result.className = "settings-result err";
          result.textContent = t("status.apiSyncFail", { error: err.message });
        } finally {
          btn.disabled = false;
          btn.textContent = t("btn.sync");
        }
      });
      tr.querySelector(".btn-api-delete").addEventListener("click", () => {
        _clearDialogState();
        _apiDeleteSourceId = a.source_id;
        _openDeleteDialog(t("status.apiDelConfirmTitle"),
          t("status.apiDelConfirmMsg", { id: a.source_id, exchange: a.exchange_label || a.exchange }));
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      // result: "data:...;base64,XXXX" → カンマ以降を取り出す
      const res = reader.result;
      const comma = res.indexOf(",");
      resolve(comma >= 0 ? res.slice(comma + 1) : res);
    };
    reader.onerror = () => reject(new Error(t("status.fileReadFail")));
    reader.readAsDataURL(file);
  });
}

// CSV インポートボタン
document.getElementById("import-csv-btn").addEventListener("click", async () => {
  const result = document.getElementById("import-csv-result");
  result.classList.add("hidden");

  const exchange = document.getElementById("import-csv-exchange").value;
  const fileInput = document.getElementById("import-csv-file");
  const sourceId = document.getElementById("import-csv-account").value.trim();

  if (!fileInput.files || fileInput.files.length === 0) {
    result.className = "settings-result err";
    result.textContent = t("status.noFile");
    result.classList.remove("hidden");
    return;
  }

  const file = fileInput.files[0];

  result.className = "settings-result ok";
  result.textContent = t("status.importRunning");
  result.classList.remove("hidden");

  try {
    const contentB64 = await readFileAsBase64(file);
    const resp = await fetch("/api/import/csv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        exchange,
        filename: file.name,
        account: sourceId || null,
        content_b64: contentB64,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const d = await resp.json();
    result.className = "settings-result ok";
    if (d.parsed === 0) {
      result.textContent = d.message || t("label.noTxFound");
    } else {
      result.textContent = t("status.importDone", { parsed: d.parsed, imported: d.imported, source: d.source });
    }
    result.classList.remove("hidden");
    fileInput.value = "";
    loadImportAccountsTable();
    loadImportBatches();
    // 取引履歴・ダッシュボードの選択肢をリセット
    _txAccountsLoaded = false;
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.importFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

// API口座登録ボタン
document.getElementById("import-api-register-btn").addEventListener("click", async () => {
  const result = document.getElementById("import-api-result");
  result.classList.add("hidden");

  const exchange = document.getElementById("import-api-exchange").value;
  const sourceId = document.getElementById("import-api-source-id").value.trim();
  const apiKey = document.getElementById("import-api-key").value.trim();
  const apiSecret = document.getElementById("import-api-secret").value.trim();
  const category = document.getElementById("import-api-category").value;

  if (!sourceId || !apiKey || !apiSecret) {
    result.className = "settings-result err";
    result.textContent = t("status.apiRequired");
    result.classList.remove("hidden");
    return;
  }

  try {
    const resp = await fetch("/api/account-apis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exchange, source_id: sourceId, api_key: apiKey, api_secret: apiSecret, category }),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);

    result.className = "settings-result ok";
    result.textContent = t("status.apiRegDone", { id: sourceId });
    result.classList.remove("hidden");

    // フォームをクリア（セキュリティのため即座に消去）
    document.getElementById("import-api-source-id").value = "";
    document.getElementById("import-api-key").value = "";
    document.getElementById("import-api-secret").value = "";

    loadApiAccountsTable();
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.apiRegFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

// 登録済みウォレット一覧テーブルを描画する
async function loadWalletsTable() {
  const tbody = document.querySelector("#import-wallets-table tbody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="loading">${t("label.loading")}</td></tr>`;
  try {
    const data = await fetchJSON("/api/wallets");
    tbody.innerHTML = "";
    if (!data.wallets || data.wallets.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("label.noWallets")}</td></tr>`;
      return;
    }
    data.wallets.forEach((w) => {
      const tr = document.createElement("tr");
      const registeredAt = w.created_at ? fmtDate(w.created_at) : "-";
      const shortAddr = w.address.length > 16
        ? `${w.address.slice(0, 8)}…${w.address.slice(-6)}` : w.address;
      tr.innerHTML = `
        <td><code style="font-family:monospace">${escapeHtml(w.source_id)}</code></td>
        <td><code style="font-family:monospace" title="${escapeHtml(w.address)}">${escapeHtml(shortAddr)}</code></td>
        <td>${escapeHtml(w.chain_label || w.chain)}</td>
        <td style="white-space:nowrap">${escapeHtml(registeredAt)}</td>
        <td style="white-space:nowrap;display:flex;gap:6px;align-items:center">
          <button class="tx-link-btn btn-wallet-sync">${t("btn.sync")}</button>
          <button class="tx-link-btn btn-wallet-delete" style="border-color:#5a2a2a">${t("btn.delete")}</button>
        </td>
      `;
      tr.querySelector(".btn-wallet-sync").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = t("btn.syncing");
        const result = document.getElementById("import-wallet-result");
        result.className = "settings-result ok";
        result.textContent = t("status.walletScanning", { id: w.source_id });
        result.classList.remove("hidden");
        try {
          const resp = await fetch(`/api/wallets/${encodeURIComponent(w.source_id)}/sync`, {
            method: "POST",
          });
          const d = await resp.json();
          if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);
          result.className = "settings-result ok";
          result.textContent = t("status.walletSyncDone", { fetched: d.fetched, imported: d.imported, id: w.source_id });
          loadImportAccountsTable();
          _txAccountsLoaded = false;
        } catch (err) {
          result.className = "settings-result err";
          result.textContent = t("status.apiSyncFail", { error: err.message });
        } finally {
          btn.disabled = false;
          btn.textContent = t("btn.sync");
        }
      });
      tr.querySelector(".btn-wallet-delete").addEventListener("click", () => {
        _clearDialogState();
        _walletDeleteSourceId = w.source_id;
        _openDeleteDialog(t("status.walletDelConfirmTitle"),
          t("status.walletDelConfirmMsg", { id: w.source_id }));
      });
      tbody.appendChild(tr);
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${t("status.error")}${escapeHtml(e.message)}</td></tr>`;
  }
}

// システム設定: スキャン用キー保存ボタン（管理者のみ）
const _systemKeysSaveBtn = document.getElementById("system-keys-save-btn");
if (_systemKeysSaveBtn) {
  _systemKeysSaveBtn.addEventListener("click", async () => {
    const result = document.getElementById("system-keys-result");
    result.classList.add("hidden");

    const etherscan = document.getElementById("system-etherscan").value.trim();
    const helius = document.getElementById("system-helius").value.trim();

    // 空欄のフィールドは送らない（＝変更なし）
    const body = {};
    if (etherscan) body.etherscan = etherscan;
    if (helius) body.helius = helius;

    if (Object.keys(body).length === 0) {
      result.className = "settings-result err";
      result.textContent = t("status.systemKeysEmpty");
      result.classList.remove("hidden");
      return;
    }

    try {
      const resp = await fetch("/api/system-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);

      result.className = "settings-result ok";
      result.textContent = t("status.systemKeysDone");
      result.classList.remove("hidden");

      document.getElementById("system-etherscan").value = "";
      document.getElementById("system-helius").value = "";
      loadSystemKeys();
    } catch (e) {
      result.className = "settings-result err";
      result.textContent = t("status.systemKeysFail", { error: e.message });
      result.classList.remove("hidden");
    }
  });
}

// ウォレット登録ボタン
document.getElementById("import-wallet-btn").addEventListener("click", async () => {
  const result = document.getElementById("import-wallet-result");
  result.classList.add("hidden");

  const address = document.getElementById("import-wallet-address").value.trim();
  const sourceId = document.getElementById("import-wallet-name").value.trim();

  if (!address) {
    result.className = "settings-result err";
    result.textContent = t("status.walletRequired");
    result.classList.remove("hidden");
    return;
  }

  try {
    const resp = await fetch("/api/wallets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        address,
        source_id: sourceId || null,
      }),
    });
    const d = await resp.json();
    if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);

    result.className = "settings-result ok";
    result.textContent = t("status.walletRegDone", { id: d.source_id, chain: d.chain_label });
    result.classList.remove("hidden");

    // フォームをクリア
    document.getElementById("import-wallet-address").value = "";
    document.getElementById("import-wallet-name").value = "";

    loadWalletsTable();
  } catch (e) {
    result.className = "settings-result err";
    result.textContent = t("status.walletRegFail", { error: e.message });
    result.classList.remove("hidden");
  }
});

// ---- メインロード ----

async function load() {
  const currency = document.getElementById("currency").value;
  const btn = document.getElementById("refresh");
  btn.classList.add("spin");
  document.getElementById("total-sub").textContent = t("label.loading");
  try {
    const [summary, sources] = await Promise.all([
      fetchJSON(`/api/summary?currency=${currency}`),
      fetchJSON(`/api/sources?currency=${currency}`),
    ]);
    renderSummary(summary);
    renderSources(sources);
    // 取引履歴のフィルタ選択肢を再構築させる（口座名変更などを反映）
    _txAccountsLoaded = false;
    const accSel = document.getElementById("tx-filter-account");
    [...accSel.options].forEach((o) => { if (o.value) o.remove(); });
  } catch (e) {
    document.getElementById("total-sub").textContent = t("status.loadError") + e.message;
  } finally {
    btn.classList.remove("spin");
  }
  // 推移グラフは独立して（summary失敗でも試みる）
  loadDashHistoryChart(_dashHistRange);
}

function getCurrentPage() {
  const active = document.querySelector(".nav-link.active[data-page]");
  return active ? active.dataset.page : "dashboard";
}

// ---- イベントリスナー ----

document.getElementById("currency").addEventListener("change", () => {
  localStorage.setItem("cs_currency", document.getElementById("currency").value);
  // 通貨に依存しない画面（取引履歴・インポート）は再読込不要。
  // それ以外（ダッシュボード・口座/資産の一覧と詳細）は現在の URL 状態のまま再描画。
  const cur = getCurrentPage();
  if (cur === "transactions" || cur === "import") return;
  router();
});

document.getElementById("refresh").addEventListener("click", () => {
  // 現在の URL 状態（詳細表示やフィルタを含む）をそのまま再描画する。
  router();
});

// ---- 全同期ボタン（口座別ページ） ----

document.getElementById("sync-all-btn").addEventListener("click", async () => {
  const btn = document.getElementById("sync-all-btn");
  const result = document.getElementById("sync-all-result");
  if (btn.disabled) return;
  btn.disabled = true;
  const origText = btn.textContent;
  btn.textContent = t("btn.syncAllRunning");
  result.className = "sync-all-result";
  result.classList.remove("hidden");
  result.textContent = t("status.syncAllRunning");
  try {
    const res = await fetch("/api/sync-all", { method: "POST" });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    if (d.total === 0) {
      result.className = "sync-all-result";
      result.textContent = t("status.syncAllNone");
    } else {
      result.className = "sync-all-result " + (d.failed === 0 ? "ok" : "err");
      let msg = t("status.syncAllDone", {
        succeeded: d.succeeded, total: d.total,
        imported: d.total_imported, fetched: d.total_fetched,
      });
      if (d.failed > 0) {
        const fails = d.results.filter((r) => !r.ok)
          .map((r) => `${r.source_id}: ${r.error}`).join(" / ");
        msg += `<div class="sync-detail">${t("status.syncAllFailed", { count: d.failed, detail: escapeHtml(fails) })}</div>`;
      }
      result.innerHTML = msg;
    }
    // 取り込み結果を画面に反映
    loadAccountsPage();
  } catch (e) {
    result.className = "sync-all-result err";
    result.textContent = t("status.syncAllError", { error: e.message });
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
});

document.getElementById("toggle-small").addEventListener("click", () => {
  showSmall = !showSmall;
  if (lastAssetsData) {
    renderAllAssets(lastAssetsData, document.getElementById("currency").value);
  }
});

document.getElementById("account-back").addEventListener("click", () => navigate("accounts"));
document.getElementById("asset-back").addEventListener("click", () => navigate("assets"));

// ダッシュボードの「すべて表示 →」リンク（口座別 / 資産別ページへ）
document.querySelectorAll("[data-nav]").forEach((el) =>
  el.addEventListener("click", () => navigate(el.dataset.nav)));

// ---- 推移グラフ レンジタブ ----

document.getElementById("dash-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadDashHistoryChart(btn.dataset.range)));

document.getElementById("acct-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadAcctHistoryChart(null, btn.dataset.range)));

document.getElementById("asset-range-tabs").querySelectorAll(".range-tab").forEach((btn) =>
  btn.addEventListener("click", () => loadAssetHistoryChart(null, btn.dataset.range)));

// ---- 初期化 ----

const saved = localStorage.getItem("cs_currency");
if (saved) document.getElementById("currency").value = saved;

// ---- 初回セットアップウィザード ----

async function runSetupIfNeeded() {
  let status;
  try {
    status = await fetchJSON("/api/setup-status");
  } catch (e) {
    return; // ネットワークエラーは無視してアプリを表示
  }
  if (!status.needs_setup) return;

  // セットアップ画面を表示
  const screen = document.getElementById("setup-screen");
  screen.classList.remove("hidden");
  document.querySelector(".layout").classList.add("hidden");
  document.getElementById("login-screen").classList.add("hidden");

  // マルチユーザーの場合のみ ADMIN_EMAILS フィールドを表示
  if (status.multi_user) {
    document.getElementById("setup-admin-section").classList.remove("hidden");
  }

  // マルチユーザーで OAuth が env 未設定なら OAuth 欄を表示・必須にし、スキップを無効化
  const oauthRequired = status.multi_user && !status.oauth_in_env;
  if (oauthRequired) {
    document.getElementById("setup-oauth-section").classList.remove("hidden");
    const skipBtn = document.getElementById("setup-skip-btn");
    const skipHint = document.querySelector("[data-i18n='setup.skipHint']");
    if (skipBtn) skipBtn.classList.add("hidden");
    if (skipHint) skipHint.classList.add("hidden");
  }

  // 生成ボタン
  document.getElementById("setup-generate-btn").addEventListener("click", async () => {
    try {
      const d = await fetchJSON("/api/generate-key");
      document.getElementById("setup-cs-key").value = d.key;
    } catch (e) {
      document.getElementById("setup-cs-key").value = t("status.error") + e.message;
    }
  });

  // 提出ボタン
  document.getElementById("setup-submit-btn").addEventListener("click", async () => {
    const result = document.getElementById("setup-result");
    result.classList.add("hidden");
    const csKey = document.getElementById("setup-cs-key").value.trim();
    const adminEmails = document.getElementById("setup-admin-emails")?.value.trim() || "";
    const baseUrl = document.getElementById("setup-base-url")?.value.trim() || "";
    const googleId = document.getElementById("setup-google-id")?.value.trim() || "";
    const googleSecret = document.getElementById("setup-google-secret")?.value.trim() || "";

    if (!csKey) {
      result.className = "settings-result err";
      result.textContent = t("setup.keyRequired");
      result.classList.remove("hidden");
      return;
    }
    if (oauthRequired && !(baseUrl && googleId && googleSecret)) {
      result.className = "settings-result err";
      result.textContent = t("setup.oauthRequired");
      result.classList.remove("hidden");
      return;
    }

    try {
      const resp = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cs_secret_key: csKey,
          admin_emails: adminEmails,
          base_url: baseUrl,
          google_client_id: googleId,
          google_client_secret: googleSecret,
        }),
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);

      result.className = "settings-result ok";
      result.textContent = t("setup.done");
      result.classList.remove("hidden");
      setTimeout(() => location.reload(), 1200);
    } catch (e) {
      result.className = "settings-result err";
      result.textContent = t("setup.failed", { error: e.message });
      result.classList.remove("hidden");
    }
  });

  // スキップボタン
  document.getElementById("setup-skip-btn").addEventListener("click", async () => {
    try {
      await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skipped: true }),
      });
    } catch (e) { /* ignore */ }
    location.reload();
  });

  // セットアップ中は他の初期化をブロック
  throw new Error("__setup__");
}

// ---- 認証チェック ----
async function checkAuth() {
  try {
    const me = await fetchJSON("/auth/me");
    if (!me.authenticated) {
      document.getElementById("login-screen").classList.remove("hidden");
      document.querySelector(".layout").classList.add("hidden");
      return false;
    }
    // ユーザー情報をサイドバーに反映
    const userInfo = document.getElementById("user-info");
    userInfo.classList.remove("hidden");
    if (me.picture) {
      document.getElementById("user-avatar").src = me.picture;
      document.getElementById("user-avatar").style.display = "";
    }
    document.getElementById("user-name").textContent = me.name || "";
    document.getElementById("user-email").textContent = me.email || "";
    _syncUserMask();
    return true;
  } catch (e) {
    return true; // ネットワークエラーは無視してアプリを表示
  }
}

// アプリ全体のメタ情報（対応通貨・管理者判定など）を読み込む。
async function loadMeta() {
  try {
    const meta = await fetchJSON("/api/meta");
    window._isAdmin = !!meta.is_admin;
  } catch (e) {
    window._isAdmin = false;
  }
}

(async () => {
  try {
    await runSetupIfNeeded();
  } catch (e) {
    if (e.message === "__setup__") return; // セットアップ画面を表示中
    // その他のエラーは無視して続行
  }
  const ok = await checkAuth();
  if (!ok) return;
  await loadMeta();
  _syncThemeBtn();
  _syncMaskBtn();
  _syncLangBtn();
  _syncCurrencyLabels();
  applyI18n();
  // アイコンをバックグラウンドで取得してから初期描画する（遅延許容）。
  await _loadCoinIcons();
  // 初期描画は現在の URL ハッシュから（直リンク・リロードで状態復元）。
  router();
})();
