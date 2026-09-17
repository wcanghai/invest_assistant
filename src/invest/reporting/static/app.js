"use strict";
const $ = (id) => document.getElementById(id);
let displayedId = 0;
const titles = {a_share_stocks:"自选股票",industry_etfs:"行业 ETF",a_share_indices:"主要指数",commodity_futures:"商品期货",us_stocks:"美股",crypto_pairs:"虚拟货币"};
const esc = (v) => String(v ?? "—").replace(/[&<>"']/g, (s) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[s]));
const num = (v, digits=2) => v == null ? "—" : Number(v).toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits});
const pct = (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : "muted"}">${v == null ? "—" : (v > 0 ? "+" : "") + num(v) + "%"}</span>`;
function table(headers, rows) { return `<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`; }
function row(cells) {return `<tr>${cells.map(c=>`<td>${c}</td>`).join("")}</tr>`;}
async function request(url, options) {const res=await fetch(url,options); const data=await res.json(); if(!res.ok) throw new Error(data.error || "请求失败"); return data;}
async function directory() {const reports=await request("/api/reports"); $("versions").innerHTML=reports.map(r=>`<option value="${r.id}">${esc(r.report_date)} · v${r.version} · ${r.status==="complete"?"完整":"部分数据"}</option>`).join(""); return reports;}
function sourceLink(r) {try {const u=new URL(r.source_url); if(["https:","http:"].includes(u.protocol)) return `<a href="${esc(u.href)}" target="_blank" rel="noopener noreferrer">来源</a>`;} catch {} return "本地";}
async function load(id) {
  try {
    const report=await request("/api/report"+(id?`?id=${id}`:"")); const d=report.data,s=d.summary;
    displayedId=Number(report.id); $("empty").hidden=true; $("content").hidden=false; $("title").textContent=`${report.report_date} 日报`;
    $("meta").textContent=`A 股交易日 ${d.a_share_date || report.report_date} · 版本 ${report.version} · 生成于 ${report.generated_at.replace("T"," ")}`;
    $("status").textContent=report.status==="complete"?"数据完整":"部分数据 / 查看缺失提示"; $("status").className="badge "+report.status;
    $("download").href=`/report.md?id=${report.id}`; $("versions").value=report.id;
    $("summary").innerHTML=[
      ["已覆盖 A 股成交额",num(s.amount_100m),"亿元 · 不等于净流入"],
      ["上涨 / 下跌",`${s.up} / ${s.down}`,`平盘 ${s.flat} · 涨跌未知 ${s.unknown}`],
      ["有效行情覆盖",`${s.covered} / ${s.expected}`,`缺失 ${s.missing} · 停牌 ${s.suspended}`],
      ["上期成交额",num(s.previous_amount_100m),`${s.previous_date || "暂无前期"} · 亿元`]
    ].map(v=>`<div class="metric"><span>${esc(v[0])}</span><strong>${esc(v[1])}</strong><small>${esc(v[2])}</small></div>`).join("");
    $("navigation").innerHTML=Object.entries(titles).map(([k,v])=>`<a href="#${k}">${v}</a>`).join("");
    $("sections").innerHTML=Object.entries(titles).map(([key,title])=>{
      const local=["a_share_stocks","industry_etfs"].includes(key);
      const headers=["名称 / 代码",local?"收盘价":"网页最新价","涨跌幅",local?"成交额（万元）":"单位",local?"三年价格分位":"报价时间","数据日期","状态 / 来源"];
      if(key==="crypto_pairs") headers[2]="24h 涨跌";
      if(key==="commodity_futures") headers[2]="相对昨结算";
      if(key==="a_share_stocks") headers.splice(5,0,"PE (TTM)","PB");
      const rows=d.sections[key].map(r=>{const cells=[
        `${esc(r.name)}<small>${esc(r.code)}</small>`,num(r.close,key==="industry_etfs"||key==="commodity_futures"?3:key==="crypto_pairs"?4:2),pct(r.pct_change),
        local?num(r.amount_10k):esc(r.unit || "USD"),
        local?`${num(r.percentile)}${r.percentile==null?"":"%"}<small>${esc(r.price_basis)}${r.percentile==null?" / 样本不足":""}</small>`:esc(r.quote_time || "—"),
        `${esc(r.source_date)}${!local?`<small>采集 ${esc((r.observed_at || "").replace("T"," "))}</small>`:""}`,
        `${r.status==="ok"?"已读取":`<span class="warn">${esc(r.error || "数据缺失")}</span>`}<small>${sourceLink(r)}</small>`
      ]; if(key==="a_share_stocks") cells.splice(5,0,num(r.pe_ttm),num(r.pb_mrq)); return row(cells);});
      const description=local?"本地数据库 · 旧版自选名单":key==="crypto_pairs"?"CoinMarketCap · 美元综合价 / 24 小时变化":key==="us_stocks"?"新浪美股 · 常规交易时段，不含盘后":key==="commodity_futures"?"新浪期货 · 具体合约 / 夜盘日期以来源为准":"新浪指数 · Playwright 网页采集";
      return `<section id="${key}"><div class="section-title"><h2>${title}</h2><span>${description}</span></div>${table(headers,rows)}</section>`;
    }).join("");
    $("markets").innerHTML=table(["市场","行情覆盖","上涨 / 下跌 / 平盘","停牌","成交额（亿元）"],(d.markets||[]).map(r=>row([esc(r.name),`${r.covered} / ${r.expected}`,`${r.up} / ${r.down} / ${r.flat}`,r.suspended,num(r.amount_100m)])));
    $("industries").innerHTML=table(["行业","有效样本","平均涨跌幅","上涨占比","成交额（亿元）"],d.industries.map(r=>row([esc(r.name),r.count,pct(r.pct_change),num(r.up_ratio)+"%",num(r.amount_100m)])));
    $("offerings").innerHTML=d.offerings.length?table(["证券","类型","申购日","申购价","申购代码"],d.offerings.map(r=>row([esc(r.security_name),esc(r.issue_type),esc(r.subscription_date),num(r.subscription_price),esc(r.subscription_code)]))):"<p class='muted'>本地暂无当前窗口的申购记录；不代表市场没有发行安排。</p>";
    $("warnings").innerHTML=d.warnings.map(w=>`<li>${esc(w)}</li>`).join("");
  } catch(e) {$("title").textContent="每日市场观察"; $("content").hidden=true; $("empty").hidden=false; $("job").textContent=e.message;}
}
let polling=false;
async function watchJob() {
  if(polling)return; polling=true;
  try {while(true){const job=await request("/api/job");$("job").textContent=job.message;$("refresh").disabled=job.running;if(!job.running){if(job.report_id && Number(job.report_id)>displayedId){await directory();await load(job.report_id);}break;} await new Promise(resolve=>setTimeout(resolve,2500));}}catch(e){$("job").textContent=e.message;$("refresh").disabled=false;}finally{polling=false;}
}
$("versions").addEventListener("change",()=>load($("versions").value));
$("refresh").addEventListener("click",async()=>{try{$("refresh").disabled=true;await request("/api/refresh",{method:"POST"});await watchJob();}catch(e){$("job").textContent=e.message;$("refresh").disabled=false;}});
directory().then(()=>load()).then(()=>watchJob()).catch(e=>{$("job").textContent=e.message;});
