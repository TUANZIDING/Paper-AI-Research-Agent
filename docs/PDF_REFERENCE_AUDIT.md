# PDF 参考文献逆向审计边界

`audit-pdf-references` 用于回答三个彼此独立的问题：PDF 中列了哪些参考文献、
正文在哪里引用了它们、开放学术 API 中是否存在高相似度的书目候选。它不能仅凭
元数据判断某篇参考文献是否支持正文论断。

## 可审计产物

- `manifest.json`：独立的 `pdf-reference-audit-0.1` 契约，记录输入 PDF 的绝对路径、
  SHA-256、字节数、页数、运行边界和输出哈希；
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
