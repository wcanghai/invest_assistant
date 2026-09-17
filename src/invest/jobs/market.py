"""每日行情同步、失败续跑与完整性核验；Windows 仅负责启动。"""

import argparse
import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path

from invest.core.locking import JobBusy, file_lock
from invest.core.files import replace_with_retry
from invest.core.settings import load_settings
from invest.jobs.status import STOCK_DOMAINS, get_status
from invest.market.security_pool.etf_data import load_etf_data
from invest.market.security_pool.main import daily_update as update_pool
from invest.market.stock.daily_helper import load_scope_codes
from invest.market.stock.main import check_data, daily_update as update_stocks
from invest.providers.security_pool import init_tdx
from invest.reporting.common import SHANGHAI


def _record(path, record):
    # 状态原子替换，日志追加同一运行编号、日期、阶段和异常原因。
    path.parent.mkdir(parents=True, exist_ok=True)
    record["updated_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    text = json.dumps(record, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    replace_with_retry(temporary, path)
    with path.with_suffix(".jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _synchronize(day, database, record, state_path, chunk_size, client=None):
    # 使用原有业务函数和参数顺序，只有完整性检查通过才标记成功。
    status = get_status(database, day)
    if status["complete"]:
        return "complete"
    record["stage"] = "calendar"
    _record(state_path, record)
    active = client if client is not None else init_tdx(script_file=__file__)
    normalized = date.fromisoformat(day).strftime("%Y%m%d")
    dates = active.get_trading_dates("SH", normalized, normalized, -1)
    if dates is None or not isinstance(dates, (list, tuple)):
        raise RuntimeError("通达信未返回有效交易日历，不能认定为休市")
    if not dates:
        # 通达信成功返回空交易日列表表示休市，接口异常由调用方记录为失败。
        return "non_trading_day"
    if normalized not in dates:
        raise RuntimeError(f"交易日历返回了非预期日期：{dates}")
    if not status["stock"]["complete"]:
        record["stage"] = "stocks"
        _record(state_path, record)
        if (status["stock"]["current_date"] or "") < day:
            record["stage"] = "security_pool"
            _record(state_path, record)
            update_pool(day, database, client=active)
            codes = load_scope_codes(database, "AllA")
        else:
            codes = status["stock"]["missing_codes"]
        if not codes:
            raise RuntimeError("证券池为空，不能认定股票更新完成")
        record["stage"] = "stocks"
        for offset in range(0, len(codes), chunk_size):
            record["batch_offset"] = offset
            _record(state_path, record)
            update_stocks(day, database, codes[offset:offset + chunk_size],
                          STOCK_DOMAINS, client=active)
        check_data(database, check_profile="daily", data_date=day)
    if not status["etf"]["complete"]:
        record["stage"] = "etfs"
        _record(state_path, record)
        load_etf_data(day, start_date=day, db_path=database, batch_size=5,
                      incremental=True, client=active)
    record["stage"] = "verify"
    _record(state_path, record)
    if not get_status(database, day)["complete"]:
        raise RuntimeError("采集结束但仍有数据未完成；下一次仅重试缺失部分")
    return "complete"


def run_market(day, database=None, state_dir=None, chunk_size=100, client=None):
    # 行情任务和日报补跑共用市场文件锁，调用者可用独立路径进行副本验证。
    settings = load_settings()
    database = Path(database or settings.databases["market"]).resolve()
    default_state = settings.path(settings.config["paths"]["state_dir"])
    if database != settings.databases["market"].resolve():
        default_state = database.parent / "state"
    state_dir = Path(state_dir or default_state)
    target = date.fromisoformat(day)
    if target > datetime.now(SHANGHAI).date():
        raise ValueError("不能更新未来日期")
    if not 1 <= chunk_size <= 100:
        raise ValueError("股票批量大小必须在 1 至 100 之间")
    record = {"run_id": uuid.uuid4().hex, "business_date": day,
              "database": str(database), "stage": "start", "status": "running"}
    state_path = state_dir / "market" / f"{day}.json"
    with file_lock(database.with_suffix(".update.lock")):
        _record(state_path, record)
        try:
            outcome = "non_trading_day" if target.weekday() >= 5 else _synchronize(
                day, database, record, state_path, chunk_size, client)
            record.update(status=outcome, stage="finished")
        except Exception as error:
            record.update(status="failed", error=str(error))
            _record(state_path, record)
            raise
        _record(state_path, record)
    return record


def main():
    # 统一入口同时支持手工补跑和定时触发，不用进程启动成功冒充数据完成。
    settings = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(SHANGHAI).date().isoformat())
    parser.add_argument("--db", type=Path, default=settings.databases["market"])
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--not-before", default=settings.config["scheduler"]["sync_not_before"])
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    cutoff = datetime.fromisoformat(f"{args.date}T{args.not_before}:00").replace(tzinfo=SHANGHAI)
    if args.guard and datetime.now(SHANGHAI) < cutoff:
        print(json.dumps({"status": "not_due", "business_date": args.date}))
        return
    if args.dry_run:
        print(json.dumps(get_status(args.db, args.date), ensure_ascii=False))
        return
    try:
        result = run_market(args.date, args.db)
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] == "complete" and not args.no_report:
            from invest.jobs.report import export_report

            export_report(args.date, source_db=args.db)
    except JobBusy as error:
        print(json.dumps({"status": "busy", "message": str(error)}, ensure_ascii=False))
        raise SystemExit(10) from error
    except Exception as error:
        print(json.dumps({"status": "failed", "message": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
