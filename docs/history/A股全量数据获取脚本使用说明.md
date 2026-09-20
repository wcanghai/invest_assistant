# A 股全量数据获取脚本使用说明

## 1. 脚本用途

脚本用于分批完成以下任务：

1. 初始化 SQLite 数据库。
2. 全量刷新 A 股证券池及相关证券池数据。
3. 按阶段获取全部股票的历史数据。
4. 输出各数据表的数据量和 SQLite 完整性检查结果。
5. 生成包含逐股票数据量的数据库情况 Markdown 报告。

脚本使用同一个 SQLite 数据库保存证券池和股票详细数据，默认路径为
`data/security_pool.db`。

## 2. 执行前准备

- 启动并登录通达信客户端 `TdxW.exe`。
- 确认通达信本地数据和接口可以正常使用。
- 执行期间不要同时运行其他 `security_pool` 或 `stock_data` 获取任务。
- 建议关闭系统自动休眠，并预留足够的磁盘空间和运行时间。

脚本默认通达信 Python 插件目录为：

```text
D:\software\tdx\PYPlugins\user
```

如实际目录不同，可通过 `-TdxPluginPath` 指定。

## 3. 先做空跑检查

空跑只检查环境并显示将要执行的命令，不会访问接口或写入数据库：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-28 `
  -DryRun
```

`EndDate` 应填写最近一个已经收盘并完成本地数据更新的交易日。

## 4. 正式执行

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-28
```

默认第一阶段从 `2004-01-01` 开始获取日线、股本、公司行为和交易指标，不获取财务报告：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-31 `
  -Phase Stage1
```

第一阶段完成后，再单独执行核心财务阶段。`Financial` 只获取第一批 92 个
核心字段，一只股票只需一次财务接口调用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-31 `
  -Phase Financial
```

只有在核心财务全部完成、且确实需要原始明细时，才执行 438 字段完整财务阶段：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-31 `
  -Phase FinancialAll
```

`Financial` 与 `FinancialAll` 使用不同检查点。已有完整财务数据在核心阶段不会
被覆盖或删除；核心字段会与原 JSON 合并保存。

如果当天证券池已经刷新，可跳过重复刷新，直接开始财务补齐：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-09-02 `
  -StartDate 2004-01-01 `
  -Phase Financial `
  -SkipSecurityPool
```

如明确需要一次获取所有数据域，可使用 `-Phase All`。默认使用已验证较稳妥的参数：

- 行情数据每批 3 只股票。
- 交易指标和财务数据每批 1 只股票。
- 全市场每25只股票形成一个外层批次，每批重新初始化通达信接口。
- 每批失败后最多重试3次，避免短暂接口异常导致整个任务报废。
- 单进程串行执行，避免多个 Python 进程争用通达信接口。

每个成功批次会把股票代码追加到日期专属检查点：

```text
data/full_load_阶段_checkpoint_起始日期_截止日期.txt
```

任务中断后重新执行相同命令，会跳过检查点中已经成功完成的股票。

如果接口环境已经验证能够稳定处理更大批次，可调整参数，例如：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\fetch_all_a_stock_data.ps1 `
  -EndDate 2026-08-28 `
  -BarBatchSize 3 `
  -MetricBatchSize 2
```

不建议在首次全量获取时盲目提高批量大小。

## 5. 输出位置

- SQLite 数据库：`data/security_pool.db`
- 执行日志：`logs/all_a_stock_full_load_时间戳.log`
- 检查点：`data/full_load_阶段_checkpoint_起始日期_截止日期.txt`
- 逐股票报告：`doc/all_a_stock_阶段_database_status_截止日期.md`

正式执行结束后，控制台和日志末尾会显示完整性检查结果与各表记录数。

## 6. 异常处理

- 任一步骤返回非零退出码时，脚本立即停止并保留日志。
- 按 `Ctrl+C` 可以中止任务，已提交到 SQLite 的批次数据会保留。
- 数据表使用更新或插入逻辑，异常后可以重新执行同一命令；已有主键记录会更新，
  不会因为重复执行产生相同主键的重复数据。
- 重新执行仍会重新请求全量范围，不等同于断点续传。
