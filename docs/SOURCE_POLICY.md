# 数据源政策与合法边界

核对日期：2026-07-26。API 政策会变化，批量或生产使用前应重新检查官方文档。

本文中的“哈希”是 hash 的规范中文术语；SHA-256 哈希指用于检测内容变化的
固定长度内容指纹，不是书写错误，也不是对原文加密。

## PubMed / NCBI E-utilities

- 角色：生物医学主题检索、PMID 与 MEDLINE 索引。
- API：<https://eutils.ncbi.nlm.nih.gov/entrez/eutils/>
- 认证：低频可不使用 key；建议设置工具名与真实联系邮箱。无 key 通常不超过
  3 请求/秒，有 key 默认不超过 10 请求/秒。
- 官方文档：
  - <https://www.ncbi.nlm.nih.gov/books/NBK25497/>
  - <https://www.ncbi.nlm.nih.gov/home/develop/api/>
- 边界：PubMed 不是全文库；摘要可能受版权保护。软件中应显著呈现 NCBI
  免责声明与版权提示。
- v0.3 执行边界：主题检索使用 History Server 建立本次 UID 集，再通过
  `WebEnv/query_key` 分页。`WebEnv` 是不透明会话句柄，不写入脱敏归档或
  SQLite 的规范化 URL；缓存只保留其不可逆身份贡献以区分不同会话。History
  固定 UID 集不等于永久快照，服务端会话也不能代替本地响应缓存。
- PubMed `CommentsCorrections` 结构化关系用于识别撤稿、更正和关注表达式。
  未返回关系不能单独证明不存在出版后通知；关系查询失败会阻断
  `citation_ready`，而不是推断安全。

## Europe PMC

- 角色：补充 PMCID、开放获取状态、生命科学预印本和全文入口。
- API：<https://www.ebi.ac.uk/europepmc/webservices/rest/>
- 官方文档：
  - <https://europepmc.org/RestfulWebService>
  - <https://europepmc.org/developers>
  - <https://europepmc.org/downloads/openaccess>
- 边界：免费访问不代表自由复制。开放全文必须逐篇检查许可证；批量任务使用
  官方 REST/OAI/FTP，不抓取主站页面。
- v0.3.1 可从已保存公开元数据提取逐篇许可证/位置候选，并在明确白名单许可证、
  已知版本和全部安全门通过时下载；候选不是最终法律判断。

## Crossref

- 角色：DOI 注册元数据核验和出版后关系线索。
- API：<https://api.crossref.org/>
- 认证：公开池无需注册；设置 `mailto` 或可联系 User-Agent 可进入 polite
  pool。必须读取当前响应头并处理 429。
- 官方文档：
  - <https://www.crossref.org/documentation/retrieve-metadata/rest-api/>
  - <https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/>
  - <https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/>
- 边界：DOI 元数据存在只证明注册记录存在，不证明研究结论正确。成员提交的
  摘要或第三方内容仍可能受版权保护。
- v0.3 使用 work metadata 与 `filter=updates:{doi}` 反向更新线索。它们与
  PubMed 关系一起进入 `citation_ready` 门控；撤稿、更正、关注表达式或查询
  不完整会阻断候选状态。Crossref 公共 REST fallback 已作为 Crossmark 相关补充
  CLI 接入，但固定不是独立实时 Crossmark dialog 核验；`unknown` 不等于“无风险”。

## SQLite 缓存与严格离线回放

- request key 基于 HTTP 方法、脱敏规范化 URL 和 `Accept`；邮箱、API key、
  token 不进入 key 的明文输入记录，明文 `WebEnv` 不持久化。
- 响应 bytes 以 SHA-256 内容寻址，并保存 HTTP 状态、取得时间、来源、ETag 和
  Last-Modified；重放读取时重新计算哈希。
- `--offline-replay` 没有网络回退。缓存缺失、blob 哈希不符或解析失败必须显式
  失败，不能静默改用实时数据。
- 离线回放复现的是已经取得的响应，不证明数据仍是最新，也不扩大原材料的
  许可证或再分发权利。
- 2026-07-26 本轮在线捕获与严格离线回放的 `records.jsonl` SHA-256 一致；
  这是单轮可重放验收结果，不是所有查询、所有故障模式或永久快照的证明。

## 全文位置与许可证边界

- 当前 `assess-license` 只评估调用者提供的 URL、许可证标识和稿件版本；它不会
  自动发现许可证，也不是最终法律或机构政策意见。
- `license_allows_machine_read=true` 只表示输入值命中保守白名单；不表示已安全
  下载、已取得正确稿件版本或允许再分发。
- 当前 `may_fetch_full_text=false`。在实现 DNS/IP、私网与重绑定防护、重定向
  重验、同源限制、robots、总时限、响应大小、内容类型和文件容器门全部通过后
  才允许自动全文下载。
- 许可证未知、冲突、仅为 `implied-oa` 或限制性许可时均失败关闭。

## 出版记录、字段溯源与版本关系

- `evidence_id` 和字段 provenance 用于回答“该值来自哪条来源观察”。v0.3
  基础只覆盖题名、DOI 和 PMID，不覆盖所有字段。
- 当前版本图只有证据必填、关系类型和环检测的数据结构与测试；尚未从真实检索
  记录自动生产预印本、接受稿、正式版和更正稿关系。
- 字段来源或版本关系不能证明论文结论。当前也没有 claim–evidence 绑定，不能
  把书目记录升级为 `claim_verified`。

## 人工核读边界

- `HUMAN_READ_SELF_ATTESTED` 固定为 `identity_verified=false`，表示未经外部
  身份验证的 CLI 自我声明。
- 事件哈希链用于发现日志改写，不证明声明者现实身份、实际核读、专业独立性、
  同意或发布批准。
- AI 和自动流水线不得代填自我声明。外部真实人工核读与身份验证仍是未完成门。

## 后续可选源

- OpenAlex：跨学科扩检和引用图谱。生产使用按当前官方政策配置免费 API key；
  元数据 CC0 不延伸至论文全文版权。
  <https://developers.openalex.org/api-reference/introduction>
- Unpaywall：按 DOI 定位合法 OA 副本，需要真实邮箱；它不是主题检索库。
  <https://unpaywall.org/api>

## 系统统一边界

1. 只通过官方 API、OAI、FTP 或明确合法的开放地址获取材料。
2. 不绕过登录、机构认证、验证码、robots、DRM 或付费墙。
3. `open_access=true` 不自动授予再分发、训练或文本挖掘权利；许可证单独判断。
4. 开放源组合不等价于 Embase、Scopus 或 Web of Science。
5. 外部元数据、摘要和全文均为不可信数据，不得影响工具权限或执行策略。
6. `citation_ready` 只表示当前书目与出版后状态门允许进入人工筛选，不表示全文
   已读、研究质量合格、claim 已核验或综合可发布。
7. 当前 6 例 golden seed、240 条合成契约用例和 v0.3.1 的 152 项测试均属于
   内部基础验收；不替代 200+ 真实黄金集、独立标注、
   真实人工核读或外部验证。
