"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "—").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toLocaleString(
  "zh-CN", {maximumFractionDigits: digits},
);
const api = async (url, options) => {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw Error(body.error || "请求失败");
  return body;
};
const labels = {momentum: "动量", trend: "趋势", low_risk: "低风险", liquidity: "流动性"};
let profile = "balanced";
let data = null;
let poll = null;

function renderOverview() {
  const summary = data.summary;
  $("overview").hidden = false;
  $("overview").innerHTML = `<article><small>行情基准日</small><strong>${esc(summary.factor_date)}</strong></article><article><small>全市场 ETF</small><strong>${fmt(summary.universe_count, 0)}</strong></article><article><small>通过股票池</small><strong>${fmt(summary.eligible_count, 0)}</strong></article><article><small>指数去重后</small><strong>${fmt(summary.deduped_count, 0)}</strong></article><article><small>有效评分</small><strong>${fmt(data.scored_count, 0)}</strong></article>`;
  $("meta").textContent = `计算于 ${data.generated_at} · ${summary.warning}`;
  $("status").textContent = "探索性结果";
}

function filtered() {
  let rows = [...data.items];
  const query = $("search").value.trim().toLowerCase();
  const underlying = $("underlying").value;
  if (query) rows = rows.filter(row => row.code.toLowerCase().includes(query)
    || row.name.toLowerCase().includes(query));
  if (underlying) rows = rows.filter(row => row.underlying_code === underlying);
  const sort = $("sort").value;
  if (sort === "return252") rows.sort((a, b) => (b.metrics.return_252 ?? -Infinity)
    - (a.metrics.return_252 ?? -Infinity));
  if (sort === "volatility") rows.sort((a, b) => (a.metrics.volatility_60 ?? Infinity)
    - (b.metrics.volatility_60 ?? Infinity));
  if (sort === "liquidity") rows.sort((a, b) => (b.metrics.adv20_10k ?? -Infinity)
    - (a.metrics.adv20_10k ?? -Infinity));
  return rows;
}

function renderRows() {
  const rows = filtered();
  $("count").textContent = `显示 ${rows.length} / ${data.items.length} 只`;
  $("rows").innerHTML = rows.map(row => `<tr data-code="${esc(row.code)}"><td>${row.rank}</td><td class="fund"><strong>${esc(row.name)}</strong><small>${esc(row.code)}</small></td><td>${esc(row.underlying_code)}</td><td class="score">${fmt(row.score, 3)}</td><td class="strength">${esc(row.strengths.join("、"))}</td><td class="risk">${esc(row.risks.join("、"))}</td><td>${fmt(row.metrics.return_20)}% / ${fmt(row.metrics.return_126)}% / ${fmt(row.metrics.return_252)}%</td><td>${fmt(row.metrics.volatility_60)}%</td><td>${fmt(row.metrics.adv20_10k)} 万元</td></tr>`).join("");
  $("rows").querySelectorAll("tr").forEach(row => {
    row.onclick = () => openDetail(row.dataset.code);
  });
}

async function load() {
  try {
    data = await api(`/api/etf-recommendations?profile=${profile}`);
    $("empty").hidden = true;
    $("results").hidden = false;
    $("list-title").textContent = `${data.profile_name} Top 20`;
    renderOverview();
    const current = $("underlying").value;
    const values = [...new Set(data.items.map(row => row.underlying_code).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, "zh-CN"));
    $("underlying").innerHTML = '<option value="">全部跟踪指数</option>'
      + values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
    $("underlying").value = values.includes(current) ? current : "";
    renderRows();
  } catch (error) {
    data = null;
    $("results").hidden = true;
    $("empty").hidden = false;
    $("job").textContent = error.message;
  }
}

function metric(label, value, suffix = "") {
  return `<div><small>${label}</small><strong>${fmt(value)}${value == null ? "" : suffix}</strong></div>`;
}

async function openDetail(code) {
  try {
    const detail = await api(`/api/etf-recommendations/detail?profile=${profile}&code=${encodeURIComponent(code)}`);
    const item = detail.item;
    const values = Object.values(item.factors).filter(value => value != null);
    const min = values.length ? Math.min(...values) : 0;
    const max = values.length ? Math.max(...values) : 1;
    const span = max - min || 1;
    const factors = Object.entries(item.factors).map(([key, value]) => {
      const width = value == null ? 0 : 10 + 80 * (value - min) / span;
      return `<div class="factor"><span>${labels[key]}</span><div class="bar"><i style="width:${width}%"></i></div><strong>${fmt(value, 3)}</strong></div>`;
    }).join("");
    $("detail-body").innerHTML = `<p class="eyebrow">${esc(detail.profile_name)} · 第 ${item.rank} 名</p><h2>${esc(item.name)} <small>${esc(item.code)}</small></h2><p>跟踪指数：${esc(item.underlying_code)}</p><p>综合分 ${fmt(item.score, 3)} · 上市日期 ${esc(item.list_date)}</p><h3>四类因子</h3><div class="factor-list">${factors}</div><h3>核心优势</h3><div class="tags">${item.strengths.map(value => `<span>${esc(value)}</span>`).join("")}</div><h3>主要风险</h3><div class="tags risks">${item.risks.map(value => `<span>${esc(value)}</span>`).join("")}</div><h3>关键指标</h3><div class="metrics">${metric("最新价", item.metrics.close)}${metric("当日涨跌", item.metrics.day_return, "%")}${metric("20日涨跌", item.metrics.return_20, "%")}${metric("60日涨跌", item.metrics.return_60, "%")}${metric("126日涨跌", item.metrics.return_126, "%")}${metric("252日涨跌", item.metrics.return_252, "%")}${metric("60日波动", item.metrics.volatility_60, "%")}${metric("120日最大回撤", item.metrics.max_drawdown_120, "%")}${metric("20日成交额", item.metrics.adv20_10k, " 万元")}${metric("253日覆盖率", item.metrics.coverage_253, "%")}</div><p class="quality">${esc(item.data_quality)}</p>`;
    $("detail").classList.add("open");
    $("detail").setAttribute("aria-hidden", "false");
    $("shade").hidden = false;
  } catch (error) {
    $("job").textContent = error.message;
  }
}

function closeDetail() {
  $("detail").classList.remove("open");
  $("detail").setAttribute("aria-hidden", "true");
  $("shade").hidden = true;
}

async function checkJob() {
  try {
    const job = await api("/api/etf-recommendations/job");
    $("job").textContent = job.error ? `${job.message}：${job.error}` : job.message;
    $("run").disabled = job.running;
    if (!job.running && poll) {
      clearInterval(poll);
      poll = null;
      await load();
    }
  } catch (error) {
    $("job").textContent = error.message;
  }
}

async function run() {
  try {
    await api("/api/etf-recommendations/run", {
      method: "POST", headers: {Origin: location.origin},
    });
    $("run").disabled = true;
    await checkJob();
    poll = setInterval(checkJob, 1200);
  } catch (error) {
    $("job").textContent = error.message;
  }
}

document.querySelectorAll("[data-profile]").forEach(button => {
  button.onclick = async () => {
    profile = button.dataset.profile;
    document.querySelectorAll("[data-profile]").forEach(item => {
      item.setAttribute("aria-selected", String(item === button));
    });
    await load();
  };
});
$("search").oninput = () => data && renderRows();
$("underlying").onchange = () => data && renderRows();
$("sort").onchange = () => data && renderRows();
$("run").onclick = run;
$("close").onclick = closeDetail;
$("shade").onclick = closeDetail;
document.onkeydown = event => { if (event.key === "Escape") closeDetail(); };
load();
checkJob();
