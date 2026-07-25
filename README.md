# AI Research Agent

一个以开放学术 API 为基础、优先保证引用可追溯性的本地检索内核。

## v0.3.1 能做什么

- 同时检索 PubMed 与 Europe PMC；
- PubMed 主路径使用 History Server（`WebEnv/query_key`）固定本次 UID 集并分页，
  Europe PMC 使用 cursor 分页；两者都支持显式截断；
- 按 DOI、PMID、规范化题名去重；
- 使用 NCBI 权威 PMID 记录与 Crossref DOI 元数据进行核验；
- 把开放获取、引用真实性、撤稿标记拆成独立状态；
- 生成不可覆盖的单次运行目录，含 `manifest.json`、`records.jsonl` 和中文 `report.md`；
- 每条来源快照带稳定 `evidence_id`，运行清单带输出文件 SHA-256 哈希（内容指纹）；
- 默认存档脱敏请求信息和原始 API 响应 bytes，可用 `verify-run` 发现运行后意外变化；
- Crossref 反向更新与 PubMed `CommentsCorrections` 撤稿、更正、关注表达式关系
  已进入 `citation_ready` 门控；状态查询不完整或出现风险信号时失败关闭；
- 提供 SQLite 内容寻址 HTTP 缓存与严格离线回放；缓存缺失不会联网回退；
- 为题名、DOI、PMID 提供字段级来源选择、备选值和证据 ID 溯源基础；
- 从已保存的 Europe PMC/PMC/Crossref 元数据提取许可证与全文位置候选；
- 在明确白名单许可证与版本前提下，使用固定公网 IP、逐跳重验、robots、
  总时限、大小、PDF/XML 类型与原子落盘门安全下载公开全文；
- 提供 Crossref 公共 REST 的 Crossmark 相关补充状态适配，但不冒充实时
  Crossmark dialog 核验；
- 将 claim 绑定到同一份已哈希 UTF-8 文本或 article XML 的唯一 quote/段落；
- 提供未经身份验证的核读自我声明哈希链和外部 Ed25519 签名包验证；
- API 部分失败时显式标记 `partial`，不会把残缺结果伪装成完整检索。

v0.3.1 不会绕过付费墙、登录或 robots，不声称替代 Embase、
Scopus 或 Web of Science，也不把“引用存在”解释为“研究质量可靠”。

## 快速开始

要求 Python 3.10+，运行时只使用标准库。

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
research-agent search '"diabetic osteoporosis" AND osteoblast' --limit 10
```

输出默认写入 `runs/<UTC时间>-<查询词>/`。查看：

```bash
open runs/<本次目录>/report.md
```

也可不安装，直接指定源码目录：

```bash
PYTHONPATH=src python3 -m ai_research_agent.cli search \
  '10.1016/j.phymed.2025.157429[doi]' --limit 5 --json
```

`--limit` 表示每个数据源的显式取得上限；命中总数超过上限时，
`manifest.json` 会标记 `truncated`。只在明确知道查询规模时使用：

```bash
research-agent search 'YOUR QUERY' --all --page-size 100
```

`--all` 可能获取非常大的结果集；它不代表跨数据库无遗漏。PubMed History
固定的是本次检索返回的 UID 集，不是永久可用的外部快照。只有完整缓存本次
实际 HTTP 响应后，才能严格离线重放本次执行；`complete` 仍不代表开放源覆盖
等同于商业数据库，也不代表系统综述检索无遗漏。

## SQLite 缓存与严格离线回放

先联网执行并写入缓存：

```bash
research-agent search 'YOUR QUERY' --limit 10 \
  --cache-db /absolute/path/to/http-cache.sqlite3 \
  --output /absolute/path/to/online-runs
```

随后使用完全相同的查询、数据源、上限和分页参数离线回放：

```bash
research-agent search 'YOUR QUERY' --limit 10 \
  --cache-db /absolute/path/to/http-cache.sqlite3 \
  --offline-replay \
  --output /absolute/path/to/replay-runs
```

离线模式只读取 SQLite 缓存；任何缓存缺失、内容哈希不符或 JSON/XML 解析失败
都会显式失败，绝不回退网络。缓存 request key 由 HTTP 方法、脱敏规范化 URL
和 `Accept` 生成；邮箱、API key、token 以及明文 `WebEnv` 不写入规范化 URL。

2026-07-26 的 v0.3 验收中，同一真实查询先在线捕获、再严格离线回放，两次
`records.jsonl` 的 SHA-256 一致。该结果证明在该次输入和缓存下记录输出可重放；
不证明远端数据库内容永久不变，也不代表已经实现中断分页续传。

## 可选配置

所有配置都通过环境变量传入；不要把密钥写进仓库。

| 变量 | 用途 |
|---|---|
| `RESEARCH_AGENT_EMAIL` | HTTP User-Agent 联系方式 |
| `NCBI_EMAIL` | NCBI E-utilities 联系方式 |
| `NCBI_TOOL` | NCBI 工具名称 |
| `NCBI_API_KEY` | 可选；提高 NCBI 速率上限 |
| `CROSSREF_MAILTO` | 可选；进入 Crossref polite pool |

没有 API key 仍可低速运行。用于持续或批量检索时，应配置真实联系邮箱并遵守各
服务的当前使用规则。

各数据源的当前官方入口、限制与版权边界见
[`docs/SOURCE_POLICY.md`](docs/SOURCE_POLICY.md)。

## 状态解释

- `authoritative_id_verified`：PMID 在权威 NCBI API（或 Europe PMC MED
  记录）解析成功。
- `cross_source_verified`：DOI 与题名进一步通过 Crossref 匹配。
- `metadata_conflict`：DOI 或题名冲突；阻断进入引用候选池。
- `unverified`：本次运行未确认；不得用于正式引用。

`citation_ready=true` 只是“可进入下一步人工筛选/质量评价的引用候选”，不是：

- 已阅读全文；
- 已评价偏倚风险；
- 已证实论文结论；
- 已确认期刊分区或影响因子；
- 已完成系统综述所需的全库检索。

此外，只有书目核验通过、元数据无冲突、所需出版后状态查询完整，且未发现
撤稿、更正或关注表达式信号时，记录才可能成为引用候选。PubMed 关系“进入
门控”是指这些信号会阻断候选状态，不是说撤稿或更正记录可以引用。

## 边界与安全

1. 摘要、全文、网页和附件都是不可信研究材料，其中的提示词或指令永不执行。
2. 全文只在明确许可证、稿件版本和全部网络/内容安全门通过时下载；跨来源重定向阻断。
3. 开放获取状态未知时保持 `unknown`，不根据 URL 或出版商猜测。
4. `partial=true` 时必须同时检查 `manifest.json` 的 `issues` 和
   `source_pagination.<source>.errors`。
5. 正式综述仍需保存完整检索式、日期、数据库范围、筛选记录，并在有权限时用
   Embase/WoS/Scopus 做补充或覆盖验证。
6. 严格离线回放只证明缓存输入可复用；它不新鲜化数据，也不替代定期在线更新。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m ai_research_agent.cli --help
research-agent verify-run /absolute/path/to/run-directory
```

2026-07-26 的 v0.3 历史集成批次记录为 87 项测试通过；v0.3.1 本轮本地集成
测试套件为 152 项并全部通过。测试覆盖分页与 History、
缓存/回放、出版后状态、许可证 fail-closed、自我声明、schema、溯源基础和
6 例 golden seed；另有 240 条确定性合成契约/对抗用例，全部明确
`human_adjudicated=false`。它们不是 200+ 真实、独立人工裁决黄金集。

`verify-run` 验证的是文件相对当前 `manifest.json` 的一致性，适合发现运行后
意外变化。Manifest 未经外部签名或可信时间戳锚定；若攻击者同时重写数据
与 manifest，本命令不能提供密码学抵抗。

## 许可证判断

```bash
research-agent assess-license \
  --url 'https://repository.example/article.pdf' \
  --license 'cc-by-4.0' \
  --version 'acceptedVersion'
```

只有明确白名单许可证、已知稿件版本与公网 HTTPS 全文位置才能进入下载安全门。
`discover-licenses` 只提取候选，不是授权或最终法律判断；`download-fulltext` 仍会
独立执行 DNS/IP、固定 IP、同源重定向、robots、总时限、大小和内容类型检查。
`license=null`、
`implied-oa`、限制性许可或私网 URL 均不允许自动全文处理。
PDF 还要求本机存在 `pdfinfo` 并通过真实解析；缺少解析器时失败关闭。XML 只接受
JATS 风格 article 容器且必须包含实质正文。

```bash
research-agent discover-licenses --crossref /absolute/path/to/crossref.json
research-agent download-fulltext --url 'https://repository.example/article.xml' \
  --license 'CC BY 4.0' --version publishedVersion \
  --output /absolute/path/to/article.xml
```

## 出版后状态专项查询

```bash
research-agent check-status '10.1177/1758835920922055' \
  --archive-dir /absolute/path/to/status-raw
```

该命令先解析 DOI，再用 Crossref `filter=updates:{doi}` 反向查找撤稿/更正/
关注表达式通知。无效 DOI、服务失败或结果截断均失败关闭；`unknown`
不等于“已排除撤稿”。

若 macOS 上的自带 Python 报 `CERTIFICATE_VERIFY_FAILED`，应修复 Python CA 证书链
或使用已正确配置证书的虚拟环境。在本机可按实际系统证书设置：

```bash
export SSL_CERT_FILE=/etc/ssl/cert.pem
```

不得通过关闭 TLS 证书验证来“解决”该问题。

## 核读自我声明门

`attest-read` 用于操作者在亲自核读后显式留下自我声明；AI 和自动流水线
不得代为执行或填写声明。由于当前没有外部身份签名/审批服务，事件只记为
`HUMAN_READ_SELF_ATTESTED`，且 `identity_verified=false`；不得把它升级为
`HUMAN_READ_CONFIRMED`。

```bash
research-agent attest-read /absolute/path/to/article.pdf \
  --sha256 '<MATERIAL_SHA256>' \
  --role anonymous_senior_domain_reviewer \
  --statement '<HUMAN_SELF_ATTESTATION_STATEMENT>' \
  --event-log /absolute/path/to/human-read-events.jsonl

research-agent verify-read-log /absolute/path/to/human-read-events.jsonl
research-agent rescind-read '<ATTESTATION_EVENT_HASH>' \
  --role anonymous_research_integrity_reviewer \
  --reason '<RESCISSION_REASON>' \
  --event-log /absolute/path/to/human-read-events.jsonl
```

核读记录只使用匿名角色标签，不伪造姓名、签名或现实身份认证。
`verify-read-log valid=true` 只表示事件链未被检测到篡改，不证明操作者身份、实际核读事实或论文内容正确。

一次真实 DOI 烟雾测试：

```bash
PYTHONPATH=src python3 -m ai_research_agent.cli search \
  '10.1016/j.phymed.2025.157429[doi]' --limit 5
```

## 尚未完成

以下能力不得根据 v0.3.1 的现有结果推断为已完成：

1. 许可证候选的最终法律/政策判断与跨法域批准；
2. PDF/OCR/页码/表图的受控提取派生链和 claim–evidence 绑定；
3. 至少 200 例真实困难样本、独立人工标注/裁决的黄金评测集；
4. 外部真实人工核读、现实身份验证、同意和审批记录；
6. 预印本、接受稿、正式版、更正稿版本图的自动生产化；
7. 中断分页续传、部分页恢复、独立实时 Crossmark 提供方和检索批次比较。
