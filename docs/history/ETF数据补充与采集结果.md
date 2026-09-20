# ETF 数据补充与采集结果

> 执行日期：2026-09-06  
> 数据截止：2026-09-04（最新有效交易日）  
> 数据库：`D:\AI_Coding_WorkSpace\invest_202609_2\data\security_pool.db`

## 1. 接口能力分析

现有 `etf_info` 使用 `get_stock_list('91')` 和 `get_trackzs_etf_info`，只能覆盖存在跟踪指数映射的 ETF，并且每日替换，只保留当前数据。接口文档中还可以补充：

| 接口 | 字段/分类 | 可补充数据 | 时间属性 |
|---|---|---|---|
| `get_stock_list` | 31、36 | 全部 ETF 清单、T+0 ETF 标记 | 当前分类 |
| `get_stock_info` | `J_start`、`underly_code` 等 | 上市日期、跟踪标的、交易单位、融资融券属性、总份额 | 当前主数据 |
| `get_market_data` | OHLCV、Amount | ETF 历史日线行情 | 历史日频 |
| `get_gpjy_value` | GP47 | ETF 申购净流入（万元） | 历史日频/事件频率 |
| `get_gpjy_value` | GP51 | 单位净值、累计净值 | 历史日频 |
| `get_gpjy_value` | GP52 | 基金份额、基金规模（万元） | 历史日频 |
| `get_scjy_value` | SC08 | 全市场 ETF 份额、净申赎（亿份） | 历史日频 |
| `get_scjy_value` | SC38 | 全市场 ETF 规模、净申赎（亿元） | 历史日频 |
| `download_file` | `down_type=2` | ETF 申赎清单、成份券篮子 | 文件/交易日 |

申赎清单属于篮子成份券级数据，需要下载和解析独立文件，本轮没有并入基础日频表。

## 2. 新增数据库表

### `etf_master`

每只 ETF 一行，保存：

- `etf_code`、`etf_name`、`exchange`；
- `list_date`、`underlying_code`、`underlying_market_code`；
- `is_t0`、`is_marginable`、`trade_unit`、`price_precision`；
- `total_shares_10k`、`first_seen_date`、`raw_info_json`。

### `etf_daily`

主键为 `(etf_code, trade_date)`，保存：

- OHLC、前收盘、涨跌幅、成交量、成交额、是否交易；
- GP47 净申购金额；
- GP51 单位净值、累计净值；
- GP52 基金份额、基金规模；
- 收盘价相对单位净值的 `premium_discount_pct`；
- 接口原始指标 JSON。

### `etf_snapshot`

按日期归档现有 `etf_info`，保存 ETF—指数映射、价格、IOPV、份额、规模和折溢价，避免每日替换后丢失历史快照。

### `etf_market_daily`

保存 SC08、SC38 的全市场 ETF 份额、规模和净申赎数据。

### `etf_data_sync_state`

记录每只 ETF 的同步截止日期，后续每日任务从上次成功日期（含当日）增量回取。

## 3. 本次实际采集结果

| 数据 | 结果 |
|---|---:|
| ETF 主表 | 1,718只 |
| T+0 ETF | 343只 |
| 有上市日期 | 1,657只 |
| 有跟踪标的 | 1,690只 |
| 融资融券标的 | 578只 |
| ETF 日频记录 | 2,009,173条 |
| 日频覆盖 ETF | 1,718只 |
| 日频日期范围 | 2005-02-23 ～ 2026-09-04 |
| 有收盘价记录 | 1,493,949条 |
| 有单位/累计净值记录 | 1,307,029条，覆盖1,632只 |
| 有份额/规模记录 | 1,345,503条，覆盖1,648只 |
| 可计算折溢价记录 | 1,295,456条 |
| 全市场 ETF 指标 | 2,000个交易日，2018-06-04 ～ 2026-08-28 |
| 当前映射快照归档 | 1,588条 |
| 同步失败 | 0只 |

2026-09-04共有1,718只ETF记录，其中1,670只有有效收盘价；其余48只主要为上市日期在9月7日或上市日期尚为空的新产品，不能伪造交易行情。

## 4. 数据源限制

1. GP47 在本机完整历史范围返回0条，因此字段已预留，但当前数据库没有净申购金额数据。
2. GP51、GP52 的最新可用日期为2026-09-02，晚于行情的发布可能存在数据源时滞。
3. SC08、SC38 最多返回最近2,000条，当前最新为2026-08-28。
4. 61只ETF缺少上市日期，28只缺少跟踪标的代码，属于上游基本信息为空。
5. `get_trackzs_etf_info` 只覆盖1,588只ETF；`etf_master` 使用ETF分类后覆盖1,718只，补齐了130只没有指数映射的ETF。

## 5. 使用方式

首次或重新补齐全部历史：

```powershell
.\scripts\fetch_etf_data.ps1 -Mode Full -StartDate 2004-01-01 -EndDate 2026-09-04
```

每日收盘后增量更新：

```powershell
.\scripts\fetch_etf_data.ps1 -Mode Daily -EndDate 2026-09-04
```

量化分析时建议：行情收益使用 `etf_daily.close`；流动性使用 `volume` 和 `amount_10k`；规模筛选使用最近可用的 `shares_10k`、`size_10k`；折溢价使用 `premium_discount_pct`。净值和规模发布日晚于行情时，应按最近已发布数据向后填充，不能向前填充。
