# 分阶段验收门

这份文件区分“已经实现”“基础已具备”和“尚未完成”。只有当前仓库中的代码、
测试或明确验收记录支持的门才能标为通过。

## G0 边界与骨架 — 通过

- README 明示不绕过付费墙、不等价替代 Embase、Scopus 或 Web of Science。
- 默认无 LLM、无付费凭据、无 API key 也可低速运行。
- 外部元数据、XML、摘要和全文被定义为不可信数据。
- 数据源政策记录官方入口、访问与版权边界。

## G1 双源发现、History 与分页 — 通过（v0.3 范围）

- PubMed 和 Europe PMC 连接器可真实联网运行。
- PubMed 主路径先以 `usehistory=y` 建立固定 UID 集，再使用
  `WebEnv/query_key` 分页取得摘要；History 响应缺字段时显式失败。
- Europe PMC 使用 cursor 分页；显式上限标记 `truncated`。
- 超时、429、5xx、无效 JSON/XML 和来源结构错误不会变成虚假的完整成功。
- 任一来源或出版后状态查询失败时记录问题；分页异常标记 `partial`。
- 原始 `WebEnv` 不写入脱敏归档或规范化 URL；缓存 request key 仍用不可逆
  指纹区分不同 History 会话，避免错误命中。

尚缺：执行中断后的分页续传、部分页恢复、超大长期会话回归，以及跨批次差异报告。

## G2 合并、去重与字段溯源 — 部分通过

- DOI/PMID/规范化题名去重已有确定性单元测试。
- DOI 与题名冲突会保留并阻断引用候选状态。
- 题名、DOI、PMID 已记录字段选择规则、选中 evidence ID、替代观察值和冲突状态。
- 版本图结构要求证据、阻断自环/有向环，并区分可能同研究与确定版本关系。

尚缺：作者、期刊、年份、全文位置和许可证等字段的完整溯源；模糊重复人工队列；
版本图自动生成、真实数据校准和生产流水线接入。

## G3 书目与出版后状态门 — 通过（当前来源范围）

- 权威 PMID 与 Crossref DOI/题名匹配可产生书目核验状态。
- 每条来源证据有稳定 `evidence_id`。
- Crossref work/reverse updates 与 PubMed publication type、
  `CommentsCorrections` 结构化关系共同参与出版后状态判断。
- 撤稿、更正或关注表达式会阻断 `citation_ready`；所需状态查询未完成也会阻断。
- 反向更新结果截断或查询失败时失败关闭；缺少信号保持 `unknown`，不写成
  “已排除撤稿”。

这里的“进入 `citation_ready`”是指关系信号已进入门控逻辑，不是说风险记录
可以进入引用候选。已有 Crossref 公共 REST fallback 的 Crossmark 相关补充适配；
它固定标记 `crossmark_realtime_verified=false`。尚缺独立实时提供方与更广泛回归样本。

## G4 输出、schema 与完整性审计 — 通过（基础版）

- 每次运行独立输出 `manifest.json`、`records.jsonl`、`report.md`。
- manifest 0.3 与 record、自我声明事件 JSON Schema 已有验证测试。
- 脱敏原始 API 响应和 HTTP 状态可存档，`verify-run` 校验文件内容指纹并发现
  非清单文件。
- 未经身份认证的核读自我声明/撤销使用 append-only JSONL 事件哈希链。
- Manifest 未经外部签名或可信时间戳锚定；同时改写数据与 manifest 的攻击者
  不在当前完整性保证范围内。

## G5 SQLite 缓存与严格离线回放 — 通过（基础版）

- request key 来自方法、脱敏规范化 URL 与 `Accept`；邮箱、API key、token 和
  明文 `WebEnv` 不落入规范化 URL。
- response bytes 以 SHA-256 内容寻址保存，并记录 HTTP 状态、取得时间、来源、
  ETag 和 Last-Modified；读取时复算哈希，blob 篡改会阻断。
- SQLite 使用 schema version、WAL、事务和并发写测试。
- `--offline-replay` 要求现有缓存；缓存缺失或损坏显式失败，绝不联网回退。
- manifest 标记 `transport_mode` 和 `cache_enabled`。

2026-07-26 本轮真实查询先在线捕获，再严格离线回放；两次
`records.jsonl` 的 SHA-256 一致。该门只证明该次输入/缓存下的重放一致性，
不等于断点续传、远端数据新鲜性或永久快照。

## G6 可靠性评测 — 合成规模门通过，人工黄金门未通过

- 当前 golden seed 共 6 例：正常、撤稿、更正待处理、关注表达式、无效 DOI、
  状态查询部分失败。
- 关键假阳性会使 seed gate 失败。
- 240 条确定性合成契约/对抗用例覆盖 8 类，每类 30 条；全部标记为
  `synthetic_contract_v1`、`human_adjudicated=false`。
- 评测门现在把漏报、状态错配、假阳性、假阴性、重复/缺失/额外 case ID 全部阻断。
- 2026-07-26 v0.3.1 本地集成套件 152 项全部通过。

240 条只验证契约与规模形状，其中同类变体为模板化重复，不能冒充 240 个独立情境。
尚缺：至少 200 例真实黄金集、独立标注/裁决、真实困难负例和持续回归。
因此不得声称系统可靠性已经得到充分外部验证。

## G7 合法全文 — 工程安全门通过，最终法律门未通过

- 许可证/位置候选可从调用方保存的 Europe PMC、PMC、Crossref 元数据提取，
  但候选固定不是法律结论或下载授权。
- 下载器要求明确白名单许可证和已知版本，默认 HTTPS，固定已验证公网 IP，阻断
  私网/混合 DNS/重绑定、跨源或降级重定向、登录/付费响应，并执行 robots、
  总时限、大小、`pdfinfo` 真实 PDF 解析、JATS XML 正文与原子落盘检查。
- Crossref 许可证版本作用域不明确时不再自动配对；未知或限制性输入失败关闭。

本轮真实 Europe PMC 下载验收在当前执行环境被安全阻断：系统 DNS 返回非公网代理
地址，下载器没有为追求成功而放宽 SSRF 门。尚缺可控公网环境验收、更多发布商格式、
许可证证据与最终 URL 的强结构化绑定，以及最终法律/政策判断。

## G8 claim–evidence 与综合 — 受限位置绑定通过，语义综合未通过

- 已支持同一份已哈希 UTF-8 文本或 article XML 的确定性文本派生、唯一 quote/
  paragraph 定位、excerpt 哈希和未经身份验证的核读事件引用。
- 材料 bytes 与平行传入文本不一致会阻断；PDF/OCR/page/section/table/figure 仍未开放。
- 当前没有 `claim_verified` 或 `synthesis_ready` 的有效升级路径。
- AI 不得以书目核验或摘要读取替代全文证据核验。

## G9 外部人工核读与身份 — 未通过

- 当前仅有 `HUMAN_READ_SELF_ATTESTED`，且 `identity_verified=false`。
- 哈希链只证明声明事件和材料指纹未被检测到改写，不证明现实身份、实际核读、
  专业独立性或发布批准。
- 可生成外部核读请求包并验证 Ed25519 detached signature、材料哈希和包完整性；
  验证使用密钥快照以避免路径替换竞态。结果仍固定 `identity_verified=false`、
  `human_read_confirmed=false`，只证明给定密钥控制。

阻断项：需要外部真实人员、受控身份/同意记录和明确审批边界。

## v0.3 之后的硬门

在以下项目完成前，本项目只能称为“v0.3 可审计开放文献检索内核”：

1. 全文许可证候选的最终法律判断和批准证据；
2. PDF/OCR/页表图的受控派生 claim–evidence 绑定；
3. 200+ 真实黄金评测集及外部标注治理；
4. 外部真实人工核读与身份验证；
5. 版本图自动生产化。
