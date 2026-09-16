# Invest

本项目是本地运行的 A 股与 ETF 数据、量化研究和市场日报平台。

使用 `python -m invest --help` 查看统一命令。首次切换前，先运行：

```powershell
python -m invest db check
python -m invest db migrate
python -m invest db verify
```

数据库位于 `data/databases/`：市场数据、研究结果、账户记录和日报各自独立。
本机通达信与 Python 路径应配置在 `config.local.toml`。

完整的迁移对照和运维步骤见 `docs/迁移与运维说明.md`。
