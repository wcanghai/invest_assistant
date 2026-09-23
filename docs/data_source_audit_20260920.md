# 数据源可行性审计

审计时间：2026-09-20T23:14:57+08:00

## 本地覆盖快照

```json
{
  "latest_dates": [
    "2026-09-18",
    "2026-09-18",
    "2026-06-30",
    "2026-09-18"
  ],
  "etf_master_coverage": [
    1727,
    1676,
    1698
  ],
  "etf_latest_fields": [
    1727,
    0,
    0,
    0,
    0
  ],
  "stock_current_fields": [
    5574,
    5574,
    5574,
    5574,
    5574
  ],
  "sector_history": [
    "2026-08-28",
    "2026-09-18",
    12,
    697
  ],
  "etf_classification": [
    8,
    8,
    8,
    0
  ],
  "etf_actions": [
    1,
    1,
    1
  ]
}
```

## 公开来源探测

状态为 HTTP 200 仅表示入口可访问；结构化字段、分页、历史完整性仍需按来源适配器单独验证。

| 来源 | 状态 | 类型 | 字节数 | 结论 |
|---|---:|---|---:|---|
| cninfo_security_map | 200 | application/json | 592101 | 可作为证券映射入口 |
| cninfo_announcement_query | 200 | application/json;charset=UTF-8 | 1492 | 可分页查询证券公告目录 |
| cninfo_pdf | 200 | application/pdf | 207271 | 可下载公告原文 |
| sse_etf_page | 200 | text/html | 28993 | 页面可访问，接口需继续定位 |
| sse_etf_list_page | 200 | text/html | 45662 | 页面可访问 |
| szse_etf_page | 200 | text/html | 11653 | 页面可访问 |
| szse_fund_notice | 200 | text/html | 93923 | 基金公告可访问 |
| szse_etf_size | 200 | application/json | 3756 | 可返回ETF规模历史记录 |
| csindex_constituents | 200 | application/vnd.ms-excel | 67072 | 可下载指数成分文件 |
| sina_company_events | 200 | text/html; charset=gbk | 28619 | 可读取公司事件辅助页 |
| fund_profile | 200 | text/html; charset=utf-8 | 52549 | 可读取ETF档案辅助页 |
| pbc_statistics | 200 | text/html | 57168 | 统计入口可访问 |
| stats_bureau | error | — | — | 普通请求受限，需备用通道 |

## 后续采集优先级

1. 巨潮公告目录与PDF：股票事件和正式投资者关系资料的主来源。
2. 深交所ETF规模/公告与中证指数成分文件：首批结构化ETF补充数据。
3. 上交所ETF申赎清单和规模：页面可访问，但需定位稳定下载接口。
4. 基金管理人资料、ETF季报持仓和费率：低频版本化采集。
5. 新闻、互动易和宏观数据：在正式来源链稳定后接入，并保留来源等级。
