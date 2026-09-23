const $=id=>document.getElementById(id);
const esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mode='security',timer,currentRun=null,runningButton=null;
const buttons=()=>[$('run'),$('industry-run'),$('screen-run'),$('compare-run')];

document.querySelectorAll('.mode').forEach(b=>b.onclick=()=>{
  if(currentRun)return;
  mode=b.dataset.mode;
  document.querySelectorAll('.mode').forEach(x=>x.classList.toggle('active',x===b));
  ['security','industry','screen','compare'].forEach(x=>$(x+'-query').hidden=x!==mode);
});

function showLogs(logs=[]){
  $('analysis-log').hidden=false;
  $('analysis-log').innerHTML=logs.map(x=>`<div class="log-line log-${esc(x.state)}"><span class="log-time">${esc((x.time||'').slice(11,19))}</span><span>${esc(x.message)}</span></div>`).join('');
}
function setRunning(button,on){
  runningButton=on?button:null;
  buttons().forEach(x=>{x.disabled=on&&x!==button});
  button.disabled=false;
  button.classList.toggle('stop',on);
  button.textContent=on?'停止分析':button.dataset.label;
}
async function stop(){
  if(!currentRun)return;
  await fetch('/api/analysis/cancel',{method:'POST',headers:{'Content-Type':'application/json','Origin':location.origin},body:JSON.stringify({run_id:currentRun})});
  $('status').textContent='本次分析已停止'; currentRun=null; setRunning(runningButton,false);
}
async function submit(body,button){
  if(currentRun){await stop();return}
  $('result').hidden=true;$('status').textContent='正在创建分析任务…';showLogs([{time:new Date().toISOString(),state:'running',message:'正在提交分析请求'}]);setRunning(button,true);
  const response=await fetch('/api/analysis/query',{method:'POST',headers:{'Content-Type':'application/json','Origin':location.origin},body:JSON.stringify(body)}),job=await response.json();
  if(!response.ok){$('status').textContent=job.error||'提交失败';setRunning(button,false);return}
  currentRun=job.run_id;
  for(let i=0;i<160&&currentRun;i++){
    await new Promise(resolve=>setTimeout(resolve,250));
    const data=await (await fetch('/api/analysis/results/'+job.run_id+'?id='+job.run_id)).json();
    showLogs(data.logs||[]);$('status').textContent='分析中：'+(data.stage||'processing');
    if(data.status==='completed'){render(data.result);$('status').textContent='分析完成';currentRun=null;setRunning(button,false);return}
    if(data.status==='failed'){$('status').textContent=data.error||'分析失败';currentRun=null;setRunning(button,false);return}
    if(data.status==='cancelled'){$('status').textContent='本次分析已停止';currentRun=null;setRunning(button,false);return}
  }
  if(currentRun){$('status').textContent='分析超时';currentRun=null;setRunning(button,false)}
}
async function search(){
  clearTimeout(timer);timer=setTimeout(async()=>{
    const q=$('code').value.trim();if(!q){$('suggestions').innerHTML='';return}
    const data=await (await fetch('/api/securities/search?q='+encodeURIComponent(q))).json();
    $('suggestions').innerHTML=(data.items||[]).map(x=>`<button data-code="${esc(x.code)}" data-kind="${x.security_type}"><b>${esc(x.code)}</b><span>${esc(x.name||x.code)}</span><small>${x.security_type==='stock'?'股票':'ETF'} · ${esc(x.exchange)}</small></button>`).join('');
    document.querySelectorAll('#suggestions button').forEach(b=>b.onclick=()=>{$('code').value=`${b.dataset.code} ${b.querySelector('span').textContent}`;$('code').dataset.code=b.dataset.code;$('kind').value=b.dataset.kind;$('suggestions').innerHTML=''});
  },180)
}
function render(d){
  $('result').hidden=false;$('security').textContent=d.security?`${d.security.name||''} ${d.security.code}`:(d.name||d.type);$('grade').textContent=d.quant_score==null?'结果已生成':d.quant_score>=70?'积极观察':d.quant_score>=50?'中性观察':'谨慎';$('quant').textContent=d.quant_score==null?'—':Number(d.quant_score).toFixed(1);const coverage=d.coverage?.ratio==null?'—':`${(d.coverage.ratio*100).toFixed(0)}%`;$('state').textContent=`本地确定性结果 · 数据覆盖率：${coverage} · 置信度：${d.confidence||'中'} · AI分：${d.ai_score??'未生成'}`;
  $('facts').innerHTML=Object.entries(d.facts||d.items?.[0]||{}).filter(([k,v])=>v!=null&&typeof v!=='object').slice(0,20).map(([k,v])=>`<div class="fact"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  $('dimensions').innerHTML=Object.entries(d.dimensions||{}).map(([k,v])=>`<div class="fact"><span>${esc(k)}</span><b>${v==null?'—':Number(v).toFixed(1)}</b></div>`).join('')||`<div class="fact"><span>候选数量</span><b>${d.count??d.items?.length??0}</b></div>`;
  $('events').innerHTML=(d.events||[]).map(e=>`<div class="event"><b>${esc(e.event_type)} · ${esc(e.direction)}</b><p>${esc(e.summary)}</p></div>`).join('')||((d.items||[]).length?d.items.slice(0,30).map(x=>`<div class="event"><b>${esc(x.code)} ${esc(x.name)}</b><p>PE ${esc(x.pe_ttm)} · ROE ${esc(x.roe_pct)} · 营收增长 ${esc(x.revenue_growth_pct)}</p></div>`).join(''):'<p class="muted">暂无结果</p>');
  $('warnings').innerHTML=(d.warnings||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li>当前未发现额外警告</li>';
}
buttons().forEach(b=>b.dataset.label=b.textContent);
$('code').oninput=()=>{$('code').dataset.code='';search()};
$('run').onclick=()=>submit({input_type:$('kind').value,code:$('code').dataset.code||$('code').value.trim().split(/\s+/)[0]},$('run'));
$('industry-run').onclick=()=>submit({input_type:'industry_theme',name:$('industry').value.trim()},$('industry-run'));
$('screen-run').onclick=()=>{const f={};for(const m of $('screen-text').value.matchAll(/(ROE|PE|PB|营收增长)\s*(≥|<=|≤|>=|>|<)\s*(\d+(?:\.\d+)?)/gi)){const k={ROE:'roe_pct',PE:'pe_ttm',PB:'pb_mrq','营收增长':'revenue_growth_pct'}[m[1]];f[k]={op:m[2].includes('<')?'lte':'gte',value:Number(m[3])}}submit({input_type:'screen',filters:f},$('screen-run'))};
$('compare-run').onclick=()=>submit({input_type:'compare',codes:$('compare-codes').value.split(/[,，\s]+/).filter(Boolean)},$('compare-run'));
