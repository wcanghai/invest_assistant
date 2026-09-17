"""在单个短事务中读取本项目行情，计算可审计的本地栏目。"""

import sqlite3
from datetime import date
from datetime import timedelta
from pathlib import Path

from invest.reporting.common import SOURCE_DB
from invest.reporting.common import number


def percentile(rows, target, adjusted=False):
    # 计算有日期上界的三年价格分位，复权因子缺失时拒绝比较。
    current = date.fromisoformat(target)
    try:
        cutoff = current.replace(year=current.year - 3)
    except ValueError:
        cutoff = current.replace(year=current.year - 3, day=28)
    samples = []
    for row in rows:
        if not cutoff.isoformat() <= row["trade_date"] <= target:
            continue
        price = number(row.get("close"))
        factor = number(row.get("forward_factor")) if adjusted else 1
        if price and price > 0 and factor and factor > 0:
            samples.append((row["trade_date"], price * factor))
    samples.sort()
    if len(samples) < 500 or samples[-1][0] != target:
        return None
    if (date.fromisoformat(samples[0][0]) - cutoff).days > 45:
        return None
    if any((date.fromisoformat(b[0]) - date.fromisoformat(a[0])).days > 45
           for a, b in zip(samples, samples[1:])):
        return None
    return round(sum(p <= samples[-1][1] for _, p in samples) / len(samples) * 100, 2)


def read_local(config, target=None, path=SOURCE_DB):
    # 从主库只读快照获取行情和证券身份，不初始化或修改主库。
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        requested = target or date.today().isoformat()
        date.fromisoformat(requested)
        if requested > date.today().isoformat():
            raise ValueError("不能生成未来日期日报")
        target = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily_bar WHERE trade_date<=?", (requested,)
        ).fetchone()[0]
        if not target:
            raise ValueError("本地数据库尚无股票日线")
        date.fromisoformat(target)
        market = [dict(row) for row in conn.execute(
            "SELECT s.*,m.list_date,b.close,b.pct_change,b.pre_close,b.amount_10k,"
            "b.is_trading,b.trade_date FROM stock_snapshot s "
            "JOIN stock_master m ON m.stock_code=s.stock_code "
            "LEFT JOIN stock_daily_bar b ON b.stock_code=s.stock_code AND b.trade_date=? "
            "WHERE s.snapshot_date=? AND s.is_active=1 AND s.is_all_a=1",
            (target, target),
        )]
        if not market:
            raise ValueError(f"{target} 缺少证券池历史快照；请先同步或选择已有交易日")
        lookup = {row["stock_code"]: row for row in market}
        sections = {}
        for category, table, column in (
            ("a_share_stocks", "stock_daily_bar", "stock_code"),
            ("industry_etfs", "etf_daily", "etf_code"),
        ):
            sections[category] = []
            for code, name in config[category].items():
                start = (date.fromisoformat(target) - timedelta(days=1100)).isoformat()
                history = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} WHERE {column}=? "
                    "AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (code, start, target),
                )]
                row = dict(history[-1]) if history else {}
                row.update(code=code, name=name, source="本地通达信数据库")
                row["source_date"] = row.get("trade_date")
                row["status"] = "ok" if row.get("trade_date") == target else "missing"
                if row["status"] != "ok":
                    row["error"] = "缺少目标交易日行情，显示最近可用值"
                row["percentile"] = percentile(history, target, category == "a_share_stocks")
                row["price_basis"] = "复权" if category == "a_share_stocks" else "未复权"
                if category == "a_share_stocks":
                    for field in ("pe_ttm", "pb_mrq", "industry_name"):
                        row[field] = lookup.get(code, {}).get(field)
                sections[category].append(row)
        end = (date.fromisoformat(requested) + timedelta(days=14)).isoformat()
        offerings = [dict(r) for r in conn.execute(
            "SELECT * FROM ipo_info WHERE subscription_date BETWEEN ? AND ? "
            "AND substr(updated_at,1,10)<=? ORDER BY subscription_date,security_code",
            (requested, end, requested),
        )]
        previous = conn.execute(
            "SELECT MAX(snapshot_date) FROM stock_snapshot WHERE snapshot_date<?", (target,)
        ).fetchone()[0]
        prior_amount = conn.execute(
            "SELECT SUM(b.amount_10k) FROM stock_daily_bar b JOIN stock_snapshot s "
            "ON s.stock_code=b.stock_code AND s.snapshot_date=b.trade_date "
            "WHERE b.trade_date=? AND s.is_active=1 AND s.is_all_a=1 "
            "AND COALESCE(s.is_suspended,0)=0 AND b.is_trading=1 AND b.close>0",
            (previous,),
        ).fetchone()[0] if previous else None
        conn.commit()
    finally:
        conn.close()
    summary, industries, missing = summarize(market, target)
    markets = []
    for exchange, name in (("SH", "沪市"), ("SZ", "深市"), ("BJ", "北交所")):
        subset = [row for row in market if row["stock_code"].endswith("." + exchange)]
        item, _, _ = summarize(subset, target)
        item["name"] = name
        markets.append(item)
    summary["previous_date"] = previous
    summary["previous_amount_100m"] = prior_amount / 10000 if prior_amount else None
    return {
        "report_date": target, "sections": sections, "summary": summary,
        "industries": industries, "missing_stocks": missing, "offerings": offerings,
        "markets": markets,
    }


def summarize(rows, target):
    # 用当日 A 股样本统计市场宽度，停牌、未上市和无效数据分别计数。
    summary = dict(expected=0, covered=0, up=0, down=0, flat=0, unknown=0,
                   suspended=0, unlisted=0, amount_100m=0, amount_missing=0)
    industries = {}
    missing = []
    for row in rows:
        if row.get("list_date") and row["list_date"] > target:
            summary["unlisted"] += 1
            continue
        if row.get("is_suspended") or row.get("is_trading") == 0:
            summary["suspended"] += 1
            continue
        summary["expected"] += 1
        price = number(row.get("close"))
        if price is None or price <= 0:
            missing.append(row["stock_code"])
            continue
        summary["covered"] += 1
        pct = number(row.get("pct_change"))
        previous = number(row.get("pre_close"))
        if pct is None and previous and previous > 0:
            pct = (price / previous - 1) * 100
        field = "unknown" if pct is None else "up" if pct > 0 else "down" if pct < 0 else "flat"
        summary[field] += 1
        amount = number(row.get("amount_10k"))
        if amount is not None and amount >= 0:
            summary["amount_100m"] += amount / 10000
        else:
            summary["amount_missing"] += 1
        name = row.get("industry_name") or "未分类"
        item = industries.setdefault(name, dict(name=name, returns=[], amount_100m=0))
        if pct is not None:
            item["returns"].append(pct)
        item["amount_100m"] += (amount or 0) / 10000
    result = []
    for item in industries.values():
        returns = item.pop("returns")
        item["count"] = len(returns)
        item["pct_change"] = sum(returns) / len(returns) if returns else None
        item["up_ratio"] = 100 * sum(r > 0 for r in returns) / len(returns) if returns else None
        result.append(item)
    result.sort(key=lambda r: r["pct_change"] if r["pct_change"] is not None else -999,
                reverse=True)
    summary["missing"] = len(missing)
    return summary, result, missing
