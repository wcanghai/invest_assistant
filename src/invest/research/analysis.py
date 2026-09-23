"""可复现的股票/ETF确定性分析，外部模型仅作为可选增强。"""
from __future__ import annotations
import math
import statistics
from datetime import date, timedelta
from uuid import uuid4
from invest.reporting.securities import bars, connect_readonly, detail
from invest.storage.research import connect, finish_run, new_run

def _num(v):
    try:
        v = float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

def _score(values):
    valid = [max(0.0, min(100.0, x)) for x in values if x is not None]
    return round(sum(valid) / len(valid), 2) if valid else None

def _linear_score(value, good, bad, higher_is_better=True):
    """将有限数值映射到0—100；缺失值保持缺失。"""
    value = _num(value)
    if value is None or good == bad:
        return None
    ratio = (value - bad) / (good - bad)
    if not higher_is_better:
        ratio = (bad - value) / (bad - good)
    return round(max(0.0, min(100.0, ratio * 100)), 2)

def _latest_stock_facts(conn, code, as_of):
    row = conn.execute("SELECT m.*,c.* FROM stock_master m LEFT JOIN stock_current c USING(stock_code) WHERE m.stock_code=?", (code,)).fetchone()
    if not row:
        return None
    payload = dict(row)
    financial = conn.execute(
        "SELECT * FROM stock_financial_report WHERE stock_code=? AND announce_date<=? "
        "ORDER BY announce_date DESC,report_date DESC,rowid DESC LIMIT 1", (code, as_of)
    ).fetchone()
    if financial:
        payload.update({k:v for k,v in dict(financial).items() if k not in {"stock_code","updated_at"}})
    trade = conn.execute("SELECT * FROM stock_trade_daily WHERE stock_code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 1", (code, as_of)).fetchone()
    if trade:
        payload.update({k:v for k,v in dict(trade).items() if k not in {"stock_code","raw_metrics_json","updated_at"}})
    payload.update(security_type="stock", code=code,
                   name=payload.get("stock_name") or payload.get("initial_name") or code)
    return payload

def _latest_etf_facts(conn, code, as_of):
    row = conn.execute(
        "SELECT e.*,d.* FROM etf_master e LEFT JOIN etf_daily d ON d.etf_code=e.etf_code "
        "AND d.trade_date=(SELECT MAX(trade_date) FROM etf_daily WHERE etf_code=e.etf_code AND trade_date<=?) "
        "WHERE e.etf_code=?", (as_of, code)
    ).fetchone()
    if not row:
        return None
    payload = dict(row)
    payload.update(security_type="etf", code=code, name=payload.get("etf_name") or code)
    return payload

def _event_rows(research_db, kind, code):
    with connect(research_db) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT event_id,event_type,direction,horizon,importance,confidence,summary,created_at "
            "FROM research_event WHERE entity_type=? AND entity_code=? ORDER BY created_at DESC LIMIT 20",
            (kind, code)).fetchall()]

def analyze(market_db, research_db, kind, code, as_of=None):
    as_of = as_of or date.today().isoformat()
    conn = connect_readonly(market_db)
    try:
        # 用户常输入六位代码；本地主表使用带交易所后缀的规范代码。
        if "." not in code:
            table, key = ("stock_master", "stock_code") if kind == "stock" else ("etf_master", "etf_code")
            found = conn.execute(f"SELECT {key} FROM {table} WHERE {key} LIKE ? ORDER BY {key} LIMIT 1", (code.zfill(6) + ".%",)).fetchone()
            if found:
                code = found[0]
        facts = _latest_stock_facts(conn, code, as_of) if kind == "stock" else _latest_etf_facts(conn, code, as_of)
        if not facts or facts.get("security_type") != kind:
            raise ValueError("证券不存在或类型不匹配")
        end = min(as_of, facts.get("latest_date") or as_of)
        start = (date.fromisoformat(end) - timedelta(days=370)).isoformat()
        history = bars(conn, code, start, end, "day")["bars"]
    finally:
        conn.close()
    evidence = []
    def fact(key, label, value, source_table="market", unit=None, formula=None, data_date=None):
        if value is not None:
            evidence.append({"id": uuid4().hex, "type": "fact", "table": source_table,
                             "key": key, "value": {"label": label, "value": value, "unit": unit,
                             "formula": formula, "security_code": code, "data_date": data_date or as_of,
                             "calculation_version": "quant-v2"}, "tier": "local"})
    close = [_num(r.get("close")) for r in history if _num(r.get("close")) is not None]
    returns = ((close[-1] / close[0]) - 1) * 100 if len(close) > 1 else None
    peak, drawdown = None, None
    if close:
        peak = close[0]; drawdown = 0.0
        for price in close:
            peak = max(peak, price); drawdown = min(drawdown, (price / peak - 1) * 100)
    volatility = None
    changes = [_num(r.get("pct_change")) for r in history]
    changes = [x for x in changes if x is not None]
    if len(changes) > 10:
        volatility = (sum((x - sum(changes)/len(changes))**2 for x in changes) / len(changes)) ** 0.5
    fact("as_of", "分析截止日", as_of)
    fact("latest_date", "本地最新行情日", facts.get("latest_date") or (history[-1]["trade_date"] if history else None))
    fact("return_1y", "近一年收益率", round(returns, 2) if returns is not None else None, unit="%", formula="last_close/first_close-1", data_date=end)
    fact("max_drawdown", "近一年最大回撤", round(drawdown, 2) if drawdown is not None else None, unit="%", formula="min(close/running_peak-1)", data_date=end)
    fact("volatility", "日收益波动率", round(volatility, 2) if volatility is not None else None, unit="%", formula="population_std(daily_pct_change)", data_date=end)
    for key in ("pe_ttm", "pb_mrq", "dividend_yield", "roe_pct", "revenue_growth_pct", "net_profit_growth_pct", "debt_ratio_pct", "size_100m", "premium_discount_pct", "underlying_code"):
        fact(key, key, facts.get(key))
    if kind == "stock":
        pe = _num(facts.get("pe_ttm")); pb = _num(facts.get("pb_mrq"))
        indicators = {
            "roe": _linear_score(facts.get("roe_pct"), 25, 0),
            "revenue_growth": _linear_score(facts.get("revenue_growth_pct"), 30, -20),
            "profit_growth": _linear_score(facts.get("net_profit_growth_pct"), 30, -30),
            "momentum_1y": _linear_score(returns, 50, -30),
            "drawdown": _linear_score(abs(drawdown) if drawdown is not None else None, 0, 50, higher_is_better=False),
            "volatility": _linear_score(volatility, 0, 5, higher_is_better=False),
            "pe": _linear_score(pe, 5, 60, higher_is_better=False) if pe and pe > 0 else None,
            "pb": _linear_score(pb, 0.5, 8, higher_is_better=False) if pb and pb > 0 else None,
        }
        dimensions = {"quality": _score([indicators["roe"]]),
                      "growth": _score([indicators["revenue_growth"], indicators["profit_growth"]]),
                      "trend": _score([indicators["momentum_1y"]]),
                      "risk": _score([indicators["drawdown"], indicators["volatility"]]),
                      "value": _score([indicators["pe"], indicators["pb"]])}
    else:
        amount = _num(facts.get("amount_10k"))
        indicators = {"momentum_1y": _linear_score(returns, 40, -25),
                      "drawdown": _linear_score(abs(drawdown) if drawdown is not None else None, 0, 40, higher_is_better=False),
                      "volatility": _linear_score(volatility, 0, 4, higher_is_better=False),
                      "amount": _linear_score(amount, 100000, 100) if amount is not None else None}
        dimensions = {"trend": _score([indicators["momentum_1y"]]),
                      "risk": _score([indicators["drawdown"], indicators["volatility"]]),
                      "liquidity": _score([indicators["amount"]]),
                      "tracking": None}
    quant = _score(list(dimensions.values()))
    events = _event_rows(research_db, kind, code)
    warnings = []
    for field in ("unit_nav", "size_100m", "premium_discount_pct") if kind == "etf" else ("pe_ttm", "roe_pct", "announce_date"):
        if facts.get(field) is None: warnings.append(f"{field} 当前缺失，未补零")
    if not events: warnings.append("暂无已缓存的官方事件")
    required = ["return_1y", "max_drawdown", "volatility"] + (["pe_ttm", "pb_mrq", "roe_pct", "revenue_growth_pct", "net_profit_growth_pct"] if kind == "stock" else ["amount_10k", "underlying_code", "unit_nav", "size_100m", "premium_discount_pct"])
    available = sum(1 for key in required if (key in {"return_1y","max_drawdown","volatility"} and {"return_1y":returns,"max_drawdown":drawdown,"volatility":volatility}[key] is not None) or facts.get(key) is not None)
    coverage = round(available / len(required), 3)
    confidence = "高" if coverage >= .85 and len(history) >= 200 else "中" if coverage >= .55 else "低"
    return {"security": {"type": kind, "code": code, "name": facts.get("name")},
            "as_of": as_of, "facts": facts, "dimensions": dimensions, "quant_score": quant,
            "indicator_scores": indicators, "coverage": {"required": len(required), "available": available, "ratio": coverage},
            "ai_score": None, "confidence": confidence if quant is not None else "低",
            "events": events, "evidence": evidence, "warnings": warnings,
            "source_status": {"local": "available", "official_external": "cached_only", "news": "auxiliary"}}

def run_analysis(market_db, research_db, kind, code, as_of=None):
    payload = {"kind": kind, "code": code, "as_of": as_of}
    run_id = new_run(kind, payload, path=research_db)
    try:
        result = analyze(market_db, research_db, kind, code, as_of)
        finish_run(run_id, result, result.get("quant_score"), None, result.get("confidence", "低"), result.get("warnings", []), path=research_db)
        result["run_id"] = run_id
        return run_id, result
    except Exception as exc:
        finish_run(run_id, {"error": str(exc)}, None, None, "低", [], path=research_db, error=str(exc))
        raise

def analyze_industry(market_db, research_db, name, as_of=None):
    as_of = as_of or date.today().isoformat()
    conn = connect_readonly(market_db)
    try:
        rows = conn.execute("SELECT m.stock_code,COALESCE(c.stock_name,m.initial_name) name,c.industry_name,c.pe_ttm,c.pb_mrq,f.roe_pct,f.revenue_growth_pct FROM stock_master m LEFT JOIN stock_current c USING(stock_code) LEFT JOIN stock_financial_report f ON f.stock_code=m.stock_code AND f.rowid=(SELECT rowid FROM stock_financial_report WHERE stock_code=m.stock_code AND announce_date<=? ORDER BY announce_date DESC,report_date DESC,rowid DESC LIMIT 1) WHERE c.industry_name LIKE ? LIMIT 200", (as_of, f"%{name}%",)).fetchall()
    finally: conn.close()
    items=[dict(r) for r in rows]; scores=[]
    for r in items:
        vals=[r.get("roe_pct"),r.get("revenue_growth_pct")]
        scores.append(_score([50+max(-50,min(50,_num(v) or 0)) for v in vals]))
    return {"type":"industry_theme","name":name,"as_of":as_of,"count":len(items),"quant_score":_score(scores),"items":items[:50],"warnings":[] if items else ["未找到匹配行业或主题"],"confidence":"中" if items else "低"}

def screen_local(market_db, filters):
    conn=connect_readonly(market_db)
    allowed={"pe_ttm","pb_mrq","roe_pct","revenue_growth_pct","net_profit_growth_pct","debt_ratio_pct","turnover_rate"}
    clauses=[]; params=[]
    for key, rule in (filters or {}).items():
        if key not in allowed or not isinstance(rule, dict) or rule.get("value") is None: continue
        op=rule.get("op","gte")
        if op not in {"gte","lte","gt","lt","eq"}: continue
        prefix = "f" if key in {"roe_pct","revenue_growth_pct","net_profit_growth_pct","debt_ratio_pct"} else "c"
        clauses.append(f"{prefix}.{key} {'>=' if op=='gte' else '<=' if op=='lte' else '>' if op=='gt' else '<' if op=='lt' else '='} ?"); params.append(rule["value"])
    where=" AND ".join(clauses) or "1=1"
    if not clauses:
        conn.close()
        raise ValueError("没有可执行的筛选条件；不支持的条件不会被静默忽略")
    rows=conn.execute(f"SELECT m.stock_code code,COALESCE(c.stock_name,m.initial_name) name,c.pe_ttm,c.pb_mrq,f.roe_pct,f.revenue_growth_pct FROM stock_master m LEFT JOIN stock_current c USING(stock_code) LEFT JOIN stock_financial_report f ON f.stock_code=m.stock_code AND f.announce_date=(SELECT MAX(announce_date) FROM stock_financial_report WHERE stock_code=m.stock_code) WHERE {where} ORDER BY f.roe_pct DESC LIMIT 100",params).fetchall(); conn.close()
    return {"type":"screen","filters":filters,"count":len(rows),"items":[dict(r) for r in rows],"warnings":[] if clauses else ["未提供可执行筛选条件"]}

def compare_local(market_db, research_db, codes):
    results=[]; conn=connect_readonly(market_db)
    for code in codes[:10]:
        clean=code.strip(); pattern=clean if "." in clean else clean.zfill(6)+".%"
        stock=conn.execute("SELECT stock_code FROM stock_master WHERE stock_code LIKE ? LIMIT 1",(pattern,)).fetchone()
        etf=conn.execute("SELECT etf_code FROM etf_master WHERE etf_code LIKE ? LIMIT 1",(pattern,)).fetchone()
        kind="stock" if stock else "etf" if etf else None
        try: results.append(analyze(market_db,research_db,kind,clean))
        except (ValueError, TypeError): results.append({"security":{"code":clean},"warnings":["证券不存在"]})
    conn.close()
    return {"type":"compare","items":results,"count":len(results)}
