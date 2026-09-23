"use strict";
(function () {
  const routes = [
    ["/", "市场日报"], ["/recommendations", "股票推荐"],
    ["/etf-recommendations", "ETF 推荐"], ["/rankings", "股票排行榜"],
    ["/securities", "证券查询"], ["/research", "研究中心"],
    ["/news", "资讯聚合"],
  ];
  function routeKey(path) {
    if (path === "/" || path === "/index.html") return "/";
    return routes.find(([route]) => route !== "/" && path.startsWith(route))?.[0];
  }
  function initNavigation() {
    const header = document.querySelector("header");
    const actions = header?.querySelector(".actions");
    if (!header || !actions) return;
    const current = routeKey(location.pathname);
    actions.id = "site-navigation";
    actions.setAttribute("aria-label", "主导航");
    routes.forEach(([href, label]) => {
      if (![...actions.querySelectorAll(":scope > a")].some(link =>
        new URL(link.href).pathname === href)) {
        const link = document.createElement("a");
        link.href = href;
        link.textContent = label;
        actions.append(link);
      }
    });
    [...actions.querySelectorAll(":scope > a")].forEach(link => {
      const href = new URL(link.href).pathname;
      if (href === current) {
        link.className = "active";
        link.setAttribute("aria-current", "page");
      }
    });
    let toggle = header.querySelector(".nav-toggle");
    if (!toggle) {
      toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "nav-toggle";
      toggle.textContent = "菜单";
      header.insertBefore(toggle, actions);
    }
    toggle.setAttribute("aria-controls", actions.id);
    toggle.setAttribute("aria-expanded", "false");
    toggle.onclick = () => {
      const open = actions.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    };
  }
  async function request(url, options = {}, timeout = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      const body = await response.json();
      if (!response.ok) throw Error(body.error || "请求失败");
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw Error("请求超时，请稍后重试");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function updateUrl(values, replace = true) {
    const url = new URL(location.href);
    Object.entries(values).forEach(([key, value]) => {
      if (value === "" || value == null || value === false) url.searchParams.delete(key);
      else url.searchParams.set(key, value === true ? "1" : String(value));
    });
    history[replace ? "replaceState" : "pushState"]({}, "", url);
  }
  function readStore(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value == null ? fallback : JSON.parse(value);
    } catch (_) { return fallback; }
  }
  function writeStore(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
  }
  window.InvestUI = {request, updateUrl, readStore, writeStore};
  initNavigation();
}());
