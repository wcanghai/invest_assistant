# Invest

本项目是本地运行的 A 股与 ETF 数据、量化研究和市场日报平台。

正式数据库已完成分库迁移。使用项目 Python 环境查看统一命令：

```powershell
.venv-report/Scripts/python.exe -m invest --help
.venv-report/Scripts/python.exe -m invest doctor
.venv-report/Scripts/python.exe -m invest jobs status --date 2026-09-17
```

数据库位于 `data/databases/`：市场数据、研究结果、账户记录和日报各自独立。
本机通达信与 Python 路径应配置在 `config.local.toml`。

首次安装使用 `scripts/install_environment.ps1`；依赖锁定于 `requirements-lock.txt`。
旧业务源码已退出根目录，保存在回滚备份中，日常请只使用 `invest` 入口。

测试源码集中归档于 `tests/unit/`，运行方式和覆盖范围见 [测试说明](tests/README.md)。

历史设计和研究文档已从 `doc/` 合并至 `docs/history/`。
目录存放规则和本次整理记录见 [目录管理](docs/目录管理.md)。

本地网站的“资讯聚合”页签读取相邻 `web_read` 项目生成的 AI 重要资讯日报，
同时展示同一业务日归档的微信文章摘要，并支持手动重跑最近已到期的 22:00 批次。项目位置可在
`config.local.toml` 的 `integrations.web_read_root` 中覆盖。

架构见 [模块职责](docs/架构与维护.md)，命令、迁移与回滚见
[运维说明](docs/迁移与运维说明.md)，最新修复证据见
[核验记录](docs/重构核验_20260918.md)。
