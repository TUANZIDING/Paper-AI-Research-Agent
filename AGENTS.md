# AGENTS.md — 给 Codex 等编码智能体的工作说明

本仓库是一个本地、可审计的文献证据命令行工具（`research-agent`）。智能体在这里
做两类事：**用它检索文献**，或**修改它的代码**。两类都必须遵守下面的边界。

## 安装与自检

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests
python3 scripts/verify_release.py
research-agent --help
```

- 离线测试不需要网络。真实检索需要网络：Codex 默认沙箱不联网，需用户在
  `~/.codex/config.toml` 的 `[sandbox_workspace_write]` 中设置 `network_access = true`，
  或逐条批准联网命令。不要为绕过沙箱而改用其他手段。
- 可选环境变量（只从用户环境读取，**绝不写入仓库、日志或提交**）：
  `RESEARCH_AGENT_EMAIL`、`NCBI_EMAIL`、`NCBI_TOOL`、`NCBI_API_KEY`、
  `CROSSREF_MAILTO`、`OPENALEX_API_KEY`、`UNPAYWALL_EMAIL`。
  未设 `UNPAYWALL_EMAIL` 时 Unpaywall 会被跳过，这是预期行为。

## 用它检索文献：常用命令

| 目的 | 命令 |
|---|---|
| PubMed + Europe PMC 双源完整检索 | `research-agent search '<布尔检索式>' --output <目录>` |
| 多来源 + 引文滚雪球至饱和 | `research-agent exhaustive '<检索式>' --require '词1\|词2*' --cache-db <缓存.sqlite> --output <目录>` |
| 中断后续跑 | 原命令加 `--resume`（同一个 `--cache-db`） |
| 下载开放许可全文 | `research-agent download-fulltext ...` 或 `exhaustive --download-oa` |
| 机构权限清单（用户本人浏览器登录） | `research-agent institution-links <运行目录> --institution-profile <仓库外的本人档案.json> --open-login` |
| 已有论文 PDF 参考文献逆向审计 | `research-agent audit-pdf-references <pdf> --output <目录>`（加 `--lookup` 才联网核对） |
| 单篇 DOI 出版后状态 | `research-agent check-status <DOI>` / `check-crossmark <DOI>` |
| claim 绑定到全文位置 | `research-agent bind-claim ...` |
| 校验运行产物哈希 | `research-agent verify-run <运行目录>` |

每次检索后都运行 `verify-run`。各命令的完整参数用 `research-agent <命令> --help` 查看。
`exhaustive` 的要点：

- `--require` 至少一组（重复即 AND，组内 `|` 为同义词，`*` 为前缀）；用
  `--exclude` / `--exclude-unless` 排除偏题方向；
- 每轮候选超过 `--max-candidates-per-round` 会在报告中显式写出，不会静默截断；
- 输出含饱和曲线、停止理由、RIS / BibTeX / ASReview CSV / PRISMA 数字。

## 汇报结果时必须遵守

- 只报告运行产物里真实存在的数字，不编造、不估算来源贡献或饱和情况。
- 明确区分：`complete` / `truncated` / `partial`；`citation_ready` 只表示可进入人工筛选，
  **不等于**已读全文、结论可靠或系统综述完整；饱和 **不等于** 检索完整；开放来源
  **不等于** Embase / Web of Science / Scopus。
- `unknown` 不得改写为“安全”；`bind-claim` 的位置命中不等于语义支持；
  智能体不得把任何记录升级为 `claim_verified` 或 `synthesis_ready`。

## 合法边界（不可协商）

- 不绕过付费墙、登录、robots 或机构权限；不使用影子图书馆或任何盗版全文源。
- 机构权限只通过 `institution-links` 在**用户本人**浏览器中打开登录页；智能体不读取、
  不保存、不填写任何账号密码，不自动下载订阅全文。
- 机构档案、缓存库、试跑结果放在仓库外，不提交。

## 修改代码时

- 新文件名带版本号、不用下划线（如 `openalexv04.py`、`EXHAUSTIVE-RETRIEVAL-v0.4.md`）；
  `README.md`、`AGENTS.md` 等约定文件名除外。
- `README.md` 与 `README.en.md` 必须同步更新。
- 新功能必须补测试；提交前全部测试与 `scripts/verify_release.py` 必须通过。
- 外部数据一律按不可信输入处理；任何新增门控默认失败关闭。
- 不提交邮箱、令牌、本机绝对路径或个人配置。
- 新功能在新分支开发，经仓库所有者确认后再合并到 `main`。
- 参考 `docs/ROADMAP.md` 的 A → B → C 顺序；D（AI 智能体层）只能读取已过门数据。
