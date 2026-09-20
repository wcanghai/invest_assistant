import json, re, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup

INDEX = 'https://www.lixinger.com/api/open-api/url-doc'
OUT = Path('lixinger_api_docs.md')
JSON_OUT = Path('lixinger_api_docs.json')
session = requests.Session()
session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; documentation-research/1.0)'
index = session.get(INDEX, timeout=30)
index.raise_for_status()
soup = BeautifulSoup(index.text, 'html.parser')
urls = sorted({a.get('href') for a in soup.select('a[href*="/api/open-api/html-doc/"]') if a.get('href')})

def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()

records=[]
for i, url in enumerate(urls, 1):
    if url.startswith('/'):
        url='https://www.lixinger.com'+url
    page = session.get(url, timeout=30)
    page.raise_for_status()
    ps = BeautifulSoup(page.text, 'html.parser')
    main = ps.find('main') or ps.body
    title = clean((main.find('h1') if main else ps.find('h1')).get_text(' ', strip=True)) if (main and main.find('h1')) or ps.find('h1') else url.rsplit('/',1)[-1]
    lines=[]
    if main:
        for el in main.find_all(['h1','h2','h3','h4','p','li','table','pre'], recursive=True):
            if el.name=='table':
                rows=[]
                for tr in el.find_all('tr'):
                    cells=[clean(c.get_text(' ', strip=True)) for c in tr.find_all(['th','td'])]
                    if cells: rows.append(cells)
                if rows:
                    lines.append({'type':'table','rows':rows})
            elif el.name=='pre':
                txt=el.get_text('\n', strip=True)
                if txt: lines.append({'type':'code','text':txt})
            else:
                txt=clean(el.get_text(' ', strip=True))
                if txt: lines.append({'type':el.name,'text':txt})
    # dedupe adjacent/repeated nested text while retaining tables/code
    ded=[]
    for x in lines:
        if ded and x==ded[-1]: continue
        ded.append(x)
    records.append({'title':title,'url':url,'content':ded})
    print(f'{i}/{len(urls)} {title}', flush=True)
    time.sleep(0.03)

md=['# 理杏仁开放平台接口文档（整理版）','',f'- 来源：[{INDEX}]({INDEX})',f'- 接口文档页数：{len(records)}','- 说明：以下内容按理杏仁官方 HTML 文档逐页整理；参数和返回字段以官方页面为准。','', '## 接口索引', '', '| 编号 | 接口 | 文档地址 |', '| --- | --- | --- |']
for n,r in enumerate(records,1):
    anchor = re.sub(r'[^a-z0-9\u4e00-\u9fff -]', '', r['title'].lower()).replace(' ','-')
    md.append(f'| {n} | [{r["title"]}](#{n}-{anchor}) | [官方页面]({r["url"]}) |')
md += ['', '## 统一格式说明', '', '每个接口按以下顺序整理：接口概述、请求信息、输入参数、输出字段、请求示例、返回示例及补充说明。原文未提供的部分不虚构内容。', '']
for n,r in enumerate(records,1):
    anchor = re.sub(r'[^a-z0-9\u4e00-\u9fff -]', '', r['title'].lower()).replace(' ','-')
    md += [f'## {n}. {r["title"]} {{#{n}-{anchor}}}', '', f'**官方文档**：[{r["url"]}]({r["url"]})', '']
    last=''
    for x in r['content']:
        if x['type']=='table':
            rows=x['rows'];
            if not rows: continue
            md.append('| '+' | '.join(rows[0])+' |'); md.append('| '+' | '.join(['---']*len(rows[0]))+' |')
            for row in rows[1:]: md.append('| '+' | '.join(row+['']*(len(rows[0])-len(row)))+' |')
            md.append('')
        elif x['type']=='code': md += ['```json',x['text'],'```','']
        else:
            prefix={'h1':'### ','h2':'### ','h3':'#### ','h4':'##### '}.get(x['type'],'')
            text=x['text']
            if text==last: continue
            # Normalize common source headings while retaining all other source text.
            normalized = {'描述:':'### 接口概述', '请求URL:':'### 请求信息', '请求方式:':'### 请求方式', '参数:':'### 输入参数', '返回数据说明:':'### 输出字段', 'API请求示例':'### 请求与返回示例'}
            text = normalized.get(text, text)
            md.append(prefix+text if prefix else ('- '+text if x['type']=='li' else text)); md.append('')
            last=text
    md.append('---'); md.append('')
OUT.write_text('\n'.join(md), encoding='utf-8')
JSON_OUT.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Wrote {OUT} and {JSON_OUT}')
