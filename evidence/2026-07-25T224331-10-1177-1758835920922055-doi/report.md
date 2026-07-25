# AI Research Agent 检索报告

- 查询：`10.1177/1758835920922055[doi]`
- 每个数据源请求上限：1
- 去重后记录：1
- 可进入引用候选池：0
- 运行是否部分失败：否
- 是否取完已启用数据源的全部分页：是
- 传输模式：`offline_replay`

## 重要边界

- 本报告只覆盖本次启用的开放 API，不等价于 Embase、Scopus 或 Web of Science 的检索覆盖。
- “可进入引用候选池”只表示标识符和元数据核验通过，不表示已人工阅读全文，也不表示研究质量合格。
- 开放获取状态与引用真实性是两条独立判断；未开放不代表文献不存在。
- 摘要、全文和外部网页内容均按不可信数据处理，不执行其中任何指令。

## 数据源结果

- pubmed: API 报告命中总数 1；实际查询：`10.1177/1758835920922055[doi]`；分页状态：`complete`；本次取得：1

## 已核验记录

### 1. Myc is a prognostic biomarker and potential therapeutic target in osteosarcoma.

- 状态：`cross_source_verified`；引用候选：否
- 作者：Feng W, Dean DC, Hornicek FJ, Spentzos D, Hoffman RM, Shi H, et al.
- 期刊/年份：Therapeutic advances in medical oncology / 2020
- 标识符：PMID 32426053; DOI 10.1177/1758835920922055
- 开放获取：`unknown`
- 撤稿标记：`flagged`
- 出版后状态：`retracted`
- 出版后状态反向查询完成：是
- 核验依据：PMID resolved in the authoritative NCBI PubMed API. DOI and title matched Crossref (title similarity 0.940). Retraction signal found; citation-ready status is blocked.
- 来源证据：
  - `ev-041a2f76384a75f20969` [pubmed](https://pubmed.ncbi.nlm.nih.gov/32426053/)
  - `ev-f1cd1728a71cbc8489a1` [crossref](https://doi.org/10.1177/1758835920922055)
  - `ev-8dd9d13da24cfe09d1ef` [crossref_status](https://api.crossref.org/works?filter=updates:10.1177/1758835920922055)
  - `ev-1909882907d5f145b30c` [pubmed_status](https://pubmed.ncbi.nlm.nih.gov/32426053/)
