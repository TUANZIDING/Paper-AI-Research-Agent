# 实施路线（v0.4.0 起）

<p align="center">
  <img src="assets/roadmap-v0.4.svg" width="100%" alt="路线图：已发布 v0.3.1、v0.3.2–0.3.3、v0.4.0；计划 A 检索可靠性、B 全文与证据、C 外部人工治理、D AI 智能体层">
</p>

**原则**：先把检索、身份核验、出版后状态和审计做成确定性内核，再让 AI 只消费
已通过明确门槛的数据。任何阶段都不得把软件自检、AI 模拟或匿名自我声明冒充
外部人工审核。

整体证据链见 [总体流程图](assets/workflow-v0.4.svg)；各门的通过条件见
[分阶段验收门](ACCEPTANCE_GATES.md)。

## 一、已发布

| 版本 | 时间 | 主题 | 主要能力 |
|---|---|---|---|
| v0.3.1 | 2026-07 | 可审计开放检索内核 | PubMed（History Server）+ Europe PMC（cursor）完整分页与显式截断；DOI/PMID/题名去重与 NCBI/Crossref 书目核验；Crossref 反向更新 + PubMed `CommentsCorrections` 出版后状态门；`evidence_id`、SHA-256、manifest/record Schema、`verify-run`；SQLite 内容寻址缓存与严格离线回放；安全全文下载器、许可证候选、claim–evidence 位置绑定、Ed25519 核读请求包；6 例 golden seed + 240 条合成契约用例 |
| v0.3.2–0.3.3 | 2026-07 → 10 | PDF 参考文献逆向审计 | 编号制 PDF 参考文献提取、正文引用页码/短上下文哈希、多字段候选评分；逐条书目查询需显式 `--lookup`；带哈希链的 checkpoint 断点续查 |
| **v0.4.0** | 2026-11-01 | 尽量穷尽检索 `exhaustive` | OpenAlex、Crossref 排序来源并入去重；多轮前后向引文滚雪球（Europe PMC / OpenAlex 引文图）与饱和停止规则、饱和曲线；关键词预筛 `--require`、排除规则 `--exclude/--exclude-unless`、每轮候选上限；合法 OA 分诊（Europe PMC OA、Unpaywall）与“需机构权限”清单；本机构档案 + `institution-links --open-login`（本人浏览器，不碰凭据）；RIS/BibTeX、ASReview CSV、PRISMA 数字；`--cache-db --resume` 续跑 |

v0.4 真实试跑（放射性骨骼肌损伤，严格骨骼肌词 + 排除低能量激光等误命中）在第 9 轮
达到代码定义的饱和。它只说明在所选引文图与关键词预筛下新增已趋零，**不等于系统综述
检索完整**。

当前产物仍只能进入“引用候选池 / 人工筛选”，不能自动进入综述正文。

## 二、下一步（按依赖顺序）

```text
A 检索可靠性与覆盖 ──► B 全文与证据生产化 ──► C 外部人工治理
                                   │
                                   └──► D AI 智能体层（只读已过门数据）
```

### A. 检索可靠性与覆盖（v0.5 优先）

1. 通用 `search` 的中断分页续传与部分页恢复（目前仅 `exhaustive` 缓存续跑和 PDF checkpoint 支持续查）；
2. 检索批次比较：显式报告新增、消失和字段变化；
3. 接入 Semantic Scholar；支持 OpenAlex API key 提高限额；
4. 本地合并 Embase、Web of Science、Scopus、知网/万方/维普的**合法导出** RIS/CSV，补商业与中文库覆盖；
5. Europe PMC 核心元数据批量化；配置联系邮箱后启用 Unpaywall，减少 OA 低估；
6. 扩大到至少 200 例真实困难样本（撤稿、更正、标识符错配、部分 API 失败、跨版本重复），区分远端漂移与软件回归。

**放行条件**：分页中断可从断点恢复且结果哈希一致；批次差异报告可复现；新增来源的截断/失败状态显式记录。

### B. 全文与证据生产化

1. 许可证证据结构化绑定到材料哈希；最终法律判断仍由人完成；
2. 更广出版商格式与可控公网环境下的真实下载验收；
3. OCR、section、表格、图片的受控提取链；
4. 自动提出预印本 → 接受稿 → 正式版 → 更正 / 撤稿通知的版本边，保留来源证据与人工裁决队列；
5. 字段级 provenance 扩展到作者、期刊、年份、全文位置、许可证与出版后状态；
6. 在 Crossref fallback 之外接入有明确契约的独立实时 Crossmark 提供方。

**放行条件**：未完成全文核读与 claim–evidence 核验时，不产生 `claim_verified` 或 `synthesis_ready`。

### C. 外部人工治理

- 200+ 条真实困难样本的独立人工标注与裁决（现有 240 条为合成用例，`human_adjudicated=false`）；
- 外部核读者的受控身份、同意、角色/资历和审批证据；需要匿名时公开层只显示角色；
- 状态区分：自我声明 → 身份已验证 → 全文已核读 → 分歧已裁决 → 允许发布；
- 撤销与裁决均为追加事件，不覆盖历史。

当前 `HUMAN_READ_SELF_ATTESTED` 为 `identity_verified=false`；外部 Ed25519 签名只证明密钥控制。

### D. AI 智能体层

AI 只读取结构化、带证据状态的记录：

| 角色 | 职责 | 不能做 |
|---|---|---|
| Literature Scout | 提出候选、补检索式 | 升级证据状态 |
| Citation Checker | 核对引用与书目记录 | 判断研究结论 |
| Evidence Grader | 在合法全文上给出可复核评价 | 替代人工核读 |
| Mechanism Mapper | 每条机制边绑定材料位置与证据 | 无证据连边 |
| Review Writer | 只引用通过指定门槛的记录 | 引用候选池外文献 |
| Devil's Advocate | 主动检索反证与覆盖缺口 | — |

外部研究循环（例如 kosmos-lite loop）以调用 `research-agent exhaustive` 的方式接入，
不并入本仓库。任何 LLM 输出都不能把状态升级为“外部人工已核读”、`claim_verified`
或 `synthesis_ready`。

## 三、明确不做

- 不绕过付费墙、登录、robots 或机构权限；不接入影子图书馆；
- 不保存或回放任何账号、密码、Cookie、令牌；不通过机构权限批量下载；
- 不把开放源检索或饱和结果宣称为系统综述级完整检索。
