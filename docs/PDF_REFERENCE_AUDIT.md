# PDF 参考文献逆向审计边界

`audit-pdf-references` 用于回答三个彼此独立的问题：PDF 中列了哪些参考文献、
正文在哪里引用了它们、开放学术 API 中是否存在高相似度的书目候选。它不能仅凭
元数据判断某篇参考文献是否支持正文论断。

## 可审计产物

- `manifest.json`：独立的 `pdf-reference-audit-0.1` 契约，只记录输入 PDF 文件名、
  SHA-256、字节数、页数、逐页派生文本哈希、提取器版本、查询策略、运行边界和输出哈希；
  默认不保存本机绝对路径，也不归档原 PDF；
- `reference_results.jsonl`：逐条解析结果、引用位置、候选书目与多字段评分；
- `citation_map.json`：引用编号、页码、短上下文及上下文 SHA-256；
- `raw_responses/`：仅在 `--lookup` 时保存脱敏后的原始 API 响应；
- `report.md`：数量、缺口和不可越过的解释边界。

运行后应执行：

```bash
research-agent verify-run /absolute/path/to/pdf-audit-run
```

## 匹配与失败关闭

候选评分使用题名、第一作者、年份、期刊和 DOI。题名相似不能单独掩盖作者或年份
冲突；中等分数进入 `candidate_requires_review`，不会自动升级为高置信匹配。
缺少参考文献标题、编号不连续、重复编号、加密 PDF、无文本扫描件或超出大小/页数
上限时，V1 会停止或明确留下未匹配状态。

## 查询批次断点续跑

仅在显式启用 `--lookup` 时，所选参考文献可写入独立 JSONL checkpoint。事件按哈希链
追加，并绑定输入载荷、所选编号和批次 ID。再次使用同一 checkpoint 时，`completed`
条目不会重复查询；`pending`、`partial` 和 `failed` 条目仍可重试。崩溃发生在
`attempt_started` 后时，该条目保持可恢复状态。

这不是通用检索分页续传，也不表示失败查询已经成功。checkpoint 被篡改、末行截断、
选择集合变化或输入载荷变化时会失败关闭。哈希链可检测链内改写，但没有外部签名或
可信时间戳，不能证明文件从未被整体重写。
批次为 `partial`、`failed` 或 `pending` 时仍会封装审计产物，但 CLI 返回退出码 3；
这使自动化不能仅凭“生成了目录”误判为查询已完成。

## 官方指南与灰色文献候选

专业组织指南可能不在 PubMed、Europe PMC 或 Crossref 的常规覆盖内。系统不根据
页面标题、Logo、meta 标签或网页自述自动认定“官方网站”。候选必须来自外部显式
登记的组织域名/路径 allowlist，并绑定登记证据来源、观察时间和内容哈希。

该机制只产生 `registered_official_source_candidate`，且固定保留
`official_identity_verified=false`、`human_read_confirmed=false`、
`final_legal_judgment=false`、`network_fetch_permitted=false`。它不证明组织现实身份、
域名控制、内容最新性或法律许可；如需抓取，必须另走独立安全门。

## 明确未证明的事项

- `matched_high_confidence` 只表示书目身份候选高度一致；
- `citation_occurrences` 只表示编号在正文中的位置；
- `semantic_support_status=NOT_ASSESSED` 表示没有完成全文 claim–evidence 核读；
- `human_adjudicated=false` 表示没有独立人工裁决；
- 单篇样稿回归是 silver-standard 验证，不是 200+ 黄金集；
- PubMed、Europe PMC 和 Crossref 不能保证覆盖指南、灰色文献或商业索引全部记录；
- 项目不绕过付费墙，也不自动给出最终法律许可判断。

对于未匹配记录，应先核对原文拼写，再访问 DOI 注册机构、期刊官网、指南发布组织
或图书馆合法入口。最终语义支持需要对已合法取得、已哈希的全文进行人工核读或受控
claim–evidence 绑定。
