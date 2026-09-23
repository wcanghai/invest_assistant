"use strict";

async function saveTracking(category, code, name, button) {
  await InvestUI.request("/api/watchlist", {
    method: "POST",
    headers: {Origin: location.origin, "Content-Type": "application/json"},
    body: JSON.stringify({
      category, code, name,
      report_date: new Date().toLocaleDateString("en-CA"),
    }),
  });
  button.textContent = "已加入";
  button.disabled = true;
}

function enhanceRecommendationRows() {
  const title = document.getElementById("list-title");
  if (title?.textContent.includes("Top 20")) {
    title.textContent = title.textContent.replace("Top 20", "Top 30");
  }
  document.querySelectorAll("#rows tr").forEach(row => {
    if (row.querySelector(".track-button")) return;
    const code = row.dataset.code || row.querySelector("small")?.textContent || "";
    const name = row.querySelector("strong")?.textContent || code;
    const cell = document.createElement("td");
    const button = document.createElement("button");
    button.className = "track-button";
    button.textContent = "加入每日跟踪";
    const category = location.pathname.includes("etf")
      ? "industry_etfs" : "a_share_stocks";
    button.onclick = () => saveTracking(category, code, name, button);
    cell.appendChild(button);
    row.appendChild(cell);
  });
}

function enhanceReport(report) {
  const sections = report?.data?.sections;
  if (!sections) return;
  Object.entries(sections).forEach(([key, items]) => {
    const table = document.querySelector(`#${key} table`);
    if (!table || table.dataset.periodReturns) return;
    table.dataset.periodReturns = "true";
    for (const label of ["近一月涨跌幅", "近半年涨跌幅"]) {
      const th = document.createElement("th");
      th.textContent = label;
      table.tHead.rows[0].appendChild(th);
    }
    items.forEach((item, index) => {
      const row = table.tBodies[0].rows[index];
      if (!row) return;
      for (const value of [item.return_1m, item.return_6m]) {
        const cell = document.createElement("td");
        cell.textContent = value == null ? "—" :
          `${value > 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
        row.appendChild(cell);
      }
    });
  });
}

async function selectedReport() {
  const query = new URLSearchParams(location.search);
  const date = query.get("date");
  const reports = await InvestUI.request("/api/reports");
  const selected = reports.filter(item => item.report_date === date)
    .sort((a, b) => Number(b.version) - Number(a.version))[0];
  return InvestUI.request(selected ? `/api/report?id=${selected.id}` : "/api/report");
}

let watchCategory = "a_share_stocks";
let watchTimer = null;
let watchItems = {};

function categoryType() {
  return watchCategory === "a_share_stocks" ? "stock" : "etf";
}

function reportDate() {
  return document.getElementById("report-date")?.value ||
    new Date().toISOString().slice(0, 10);
}

function watchRow(code, name, action, disabled = false) {
  const item = document.createElement("div");
  item.className = "watchlist-item";
  const label = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = name || code;
  const small = document.createElement("small");
  small.textContent = code;
  label.append(strong, small);
  const button = document.createElement("button");
  button.textContent = disabled ? "已在自选中" : action;
  button.disabled = disabled;
  item.append(label, button);
  return {item, button};
}

async function refreshWatchItems() {
  const data = await InvestUI.request(`/api/watchlist?category=${watchCategory}`);
  watchItems = data.items[watchCategory] || {};
  const target = document.getElementById("watchlist-current");
  target.replaceChildren();
  Object.entries(watchItems).forEach(([code, name]) => {
    const row = watchRow(code, name, "删除");
    row.button.classList.add("danger");
    row.button.onclick = async () => {
      if (!confirm(`确定从每日跟踪中删除 ${name}（${code}）吗？`)) return;
      await mutateWatchlist("DELETE", code, name);
    };
    target.appendChild(row.item);
  });
  if (!target.children.length) target.textContent = "当前没有自选标的。";
}

async function mutateWatchlist(method, code, name) {
  const message = document.getElementById("watchlist-message");
  message.textContent = method === "DELETE" ? "正在删除并更新日报…" :
    "正在加入并更新日报…";
  try {
    const result = await InvestUI.request("/api/watchlist", {
      method,
      headers: {Origin: location.origin, "Content-Type": "application/json"},
      body: JSON.stringify({
        category: watchCategory, code, name, report_date: reportDate(),
      }),
    });
    await refreshWatchItems();
    if (typeof directory === "function") await directory();
    if (typeof load === "function") await load(result.report_id);
    message.textContent = method === "DELETE" ? "已删除并更新日报。" :
      "已加入并更新日报。";
    if (method !== "DELETE") document.getElementById("watchlist-search").value = "";
    document.getElementById("watchlist-results").replaceChildren();
  } catch (error) {
    message.textContent = error.message;
  }
}

async function searchWatchlist() {
  const input = document.getElementById("watchlist-search");
  const target = document.getElementById("watchlist-results");
  const query = input.value.trim();
  target.replaceChildren();
  if (!query) return;
  try {
    const data = await InvestUI.request(
      `/api/securities/search?q=${encodeURIComponent(query)}`,
    );
    const rows = data.items.filter(item => item.security_type === categoryType());
    rows.forEach(item => {
      const exists = Object.hasOwn(watchItems, item.code);
      const row = watchRow(item.code, item.name, "加入", exists);
      row.button.onclick = () => mutateWatchlist("POST", item.code, item.name);
      target.appendChild(row.item);
    });
    if (!rows.length) target.textContent = "没有找到匹配的标的。";
  } catch (error) {
    document.getElementById("watchlist-message").textContent = error.message;
  }
}

function showWatchTab(manage) {
  document.getElementById("watchlist-add-tab").setAttribute(
    "aria-selected", String(!manage),
  );
  document.getElementById("watchlist-manage-tab").setAttribute(
    "aria-selected", String(manage),
  );
  document.getElementById("watchlist-add-panel").hidden = manage;
  document.getElementById("watchlist-manage-panel").hidden = !manage;
  if (manage) refreshWatchItems();
}

async function openWatchlist(category) {
  watchCategory = category;
  document.getElementById("watchlist-title").textContent =
    category === "a_share_stocks" ? "管理自选股票" : "管理自选 ETF";
  document.getElementById("watchlist-drawer").classList.add("open");
  document.getElementById("watchlist-drawer").setAttribute("aria-hidden", "false");
  document.getElementById("watchlist-shade").hidden = false;
  document.getElementById("watchlist-message").textContent = "";
  showWatchTab(false);
  await refreshWatchItems();
  document.getElementById("watchlist-search").focus();
}

function closeWatchlist() {
  document.getElementById("watchlist-drawer").classList.remove("open");
  document.getElementById("watchlist-drawer").setAttribute("aria-hidden", "true");
  document.getElementById("watchlist-shade").hidden = true;
}

function injectWatchlistButtons() {
  for (const category of ["a_share_stocks", "industry_etfs"]) {
    const title = document.querySelector(`#${category} .section-title`);
    if (!title || title.querySelector(".add-watchlist")) continue;
    const button = document.createElement("button");
    button.className = "add-watchlist";
    button.textContent = "增加自选";
    button.onclick = () => openWatchlist(category);
    title.appendChild(button);
  }
}

function setupWatchlistUi() {
  const search = document.getElementById("watchlist-search");
  if (!search) return;
  search.oninput = () => {
    clearTimeout(watchTimer);
    watchTimer = setTimeout(searchWatchlist, 250);
  };
  document.getElementById("watchlist-add-tab").onclick = () => showWatchTab(false);
  document.getElementById("watchlist-manage-tab").onclick = () => showWatchTab(true);
  document.getElementById("watchlist-close").onclick = closeWatchlist;
  document.getElementById("watchlist-shade").onclick = closeWatchlist;
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") closeWatchlist();
  });
}

new MutationObserver(enhanceRecommendationRows).observe(document.body, {
  subtree: true, childList: true,
});
enhanceRecommendationRows();
setTimeout(enhanceRecommendationRows, 1500);
if (location.pathname === "/" || location.pathname === "/index.html") {
  setupWatchlistUi();
  new MutationObserver(injectWatchlistButtons).observe(
    document.getElementById("sections"), {subtree: true, childList: true},
  );
  injectWatchlistButtons();
  selectedReport().then(report => {
    const apply = () => enhanceReport(report);
    apply();
    new MutationObserver(apply).observe(document.getElementById("sections"), {
      subtree: true, childList: true,
    });
  });
}
