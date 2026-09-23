"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const categories = ["全部", "产业商业", "政策安全", "模型研究", "产品工具"];
let report = null;
let activeCategory = "全部";
let pollTimer = null;

function safeUrl(value) {
  try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; }
  catch (_) { return ""; }
}

function localTime(value) {
  if (!value) return "时间待核实";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "时间待核实";
  return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(date);
}

function sourceLinks(item) {
  const values = item.sources?.length ? item.sources : [{name: item.sourceName, url: item.url}];
  return values.map(source => {
    const url = safeUrl(source.url);
    const label = esc(source.name || "原文");
    return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  }).join(" · ");
}

function renderCards() {
  const items = report.selected.filter(item => activeCategory === "全部" ||
    item.category === activeCategory);
  $("count").textContent = `显示 ${items.length} / ${report.selected.length} 条`;
  $("cards").innerHTML = items.map((item, index) => `<article class="news-card ${
    ["产业商业", "政策安全"].includes(item.category) ? "investment" : ""}">
      <div class="card-top"><span class="rank">${String(index + 1).padStart(2, "0")}</span>
        <span class="category">${esc(item.category || "其他")}</span>
        <span>${esc(item.status || "状态待核实")}</span><time>${esc(localTime(item.time?.iso))}</time></div>
      <h3>${esc(item.title)}</h3><p>${esc(item.summary || "暂无摘要")}</p>
      <p class="why"><strong>为什么重要</strong>${esc(item.why || "暂无重要性说明")}</p>
      ${item.updateNote ? `<p class="update"><strong>新增进展</strong>${esc(item.updateNote)}</p>` : ""}
      <footer><span>来源：${sourceLinks(item)}</span>${Number.isFinite(item.score) ?
        `<span>重要性 ${esc(item.score)} / 20</span>` : ""}</footer></article>`).join("") ||
    '<div class="empty-inline">该分类暂无严格精选。</div>';
}

function renderFilters() {
  $("filters").innerHTML = categories.map(category => `<button role="tab" data-category="${category}"
    aria-selected="${category === activeCategory}">${category}</button>`).join("");
  $("filters").querySelectorAll("button").forEach(button => {
    button.onclick = () => { activeCategory = button.dataset.category; renderFilters(); renderCards(); };
  });
}

function renderSources() {
  $("sources").innerHTML = report.sources.map(source => `<article class="source ${esc(source.state)}">
    <div><strong>${esc(source.name)}</strong><span>${esc(source.state === "ok" ? "正常" :
      source.state === "no_updates" ? "窗口内无更新" : source.state === "partial" ?
      "覆盖可能不完整" : "暂时不可用")}</span></div>
    <p>窗口内 ${esc(source.inWindow ?? 0)} 篇 · 已采集 ${esc(source.collected ?? 0)} 篇</p>
    ${source.stopReasons?.length ? `<small>${esc(source.stopReasons.join("；"))}</small>` : ""}</article>`).join("");
}

function renderWechat() {
  const wechat = report.wechat || {items: [], metrics: {}, status: "尚无归档"};
  const metrics = wechat.metrics || {};
  $("wechat-meta").textContent = `${wechat.status} · 已归档 ${wechat.items.length} 篇${
    Number.isFinite(metrics.candidates) ? ` / 候选 ${metrics.candidates} 篇` : ""}`;
  $("wechat-cards").innerHTML = wechat.items.map(item => {
    const url = safeUrl(item.source_url);
    const title = url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${
      esc(item.title)}</a>` : esc(item.title);
    return `<article class="wechat-card"><div class="wechat-meta"><span>${esc(
      item.author || "公众号待核实")}</span><time>${esc(item.publish_time || "时间待核实")}</time></div>
      <h3>${title}</h3><p class="wechat-conclusion">${esc(
        item.conclusion || "暂无一句话结论")}</p>${item.summary ? `<details><summary>查看核心内容</summary><p>${
        esc(item.summary)}</p></details>` : ""}${item.keywords?.length ? `<div class="keywords">${
        item.keywords.map(keyword => `<span>${esc(keyword)}</span>`).join("")}</div>` : ""}</article>`;
  }).join("") || '<div class="empty-inline">该日期尚无已归档的微信文章。</div>';
}

function renderReport() {
  $("empty").hidden = true; $("content").hidden = false;
  $("title").textContent = `${report.date} 重要资讯`;
  $("meta").textContent = `北京时间窗口 · ${new Date(report.window.start).toLocaleString("zh-CN",
    {timeZone: "Asia/Shanghai"})} 至 ${new Date(report.window.end).toLocaleString("zh-CN",
    {timeZone: "Asia/Shanghai"})} · 生成于 ${localTime(report.generated_at)}`;
  const partial = report.state !== "complete";
  $("status").textContent = `${partial ? "部分完成" : "完整"} · ${report.selected.length} 条精选`;
  $("status").className = `badge ${partial ? "warning" : "success"}`;
  $("notice").hidden = !partial;
  $("notice").innerHTML = partial ? "<strong>覆盖提示</strong><span>部分来源访问受限或分析未完成，已展示经过核验的可用结果。</span>" : "";
  $("overview").innerHTML = report.overview.map(item => `<li>${esc(item)}</li>`).join("");
  renderWechat(); renderFilters(); renderCards(); renderSources();
  $("extras").innerHTML = report.extras.map(item => { const url = safeUrl(item.url);
    return `<li>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a>` : esc(item.title)}<span>${esc(item.sourceName || "")}</span></li>`;
  }).join("") || "<li>本次没有补充阅读。</li>";
  $("errors-section").hidden = !report.errors.length;
  $("errors").innerHTML = report.errors.map(item => `<li>${esc(item)}</li>`).join("");
}

async function loadDates(selected) {
  const data = await InvestUI.request("/api/news/dates");
  $("news-date").innerHTML = data.items.map(item => `<option value="${item.date}">${item.date} · ${
    item.state === "complete" ? "完整" : "部分"} · ${item.selected_count} 条</option>`).join("");
  if (selected && data.items.some(item => item.date === selected)) $("news-date").value = selected;
}

async function load(date) {
  $("job").textContent = "正在读取资讯…";
  try {
    await loadDates(date);
    report = await InvestUI.request(`/api/news${date ? `?date=${encodeURIComponent(date)}` : ""}`);
    $("news-date").value = report.date; activeCategory = "全部"; renderReport();
    InvestUI.updateUrl({date: report.date}); $("job").textContent = "";
  } catch (error) {
    $("content").hidden = true; $("empty").hidden = false;
    $("job").textContent = error.message; $("status").textContent = "暂不可用";
    $("status").className = "badge warning";
  }
}

async function pollJob() {
  try {
    const job = await InvestUI.request("/api/news/job");
    $("job").textContent = job.message || ""; $("refresh").disabled = Boolean(job.running);
    if (job.running) pollTimer = setTimeout(pollJob, 2500);
    else if (job.stage === "done") await load(job.date);
  } catch (error) { $("job").textContent = error.message; $("refresh").disabled = false; }
}

$("news-date").onchange = () => load($("news-date").value);
$("refresh").onclick = async () => {
  clearTimeout(pollTimer); $("refresh").disabled = true;
  try {
    const job = await InvestUI.request("/api/news/refresh", {method: "POST"}, 20000);
    $("job").textContent = job.message; pollTimer = setTimeout(pollJob, 1000);
  } catch (error) { $("job").textContent = error.message; $("refresh").disabled = false; }
};

load(new URL(location.href).searchParams.get("date"));
