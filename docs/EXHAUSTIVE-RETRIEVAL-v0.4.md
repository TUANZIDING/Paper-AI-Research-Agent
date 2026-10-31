# 穷尽文献检索工作流（v0.4） / Exhaustive retrieval workflow

> 目标是“在开放来源内尽量不漏”，并把每一步的覆盖范围、截断和停止理由写清楚。
> 它**不等于**系统综述检索完整：Embase、Web of Science、Scopus、知网/万方/维普、
> 灰色文献和人工筛选仍然需要。

## 1. 流程

```text
数据库检索（PubMed、Europe PMC：布尔式）
  + 相关性排序来源（OpenAlex、Crossref：自由文本，强制上限）
  → 合并去重（DOI / PMID / 标准化题名）
  → 摘要补全（Europe PMC core）
  → 引文滚雪球：种子 → 后向参考文献 + 前向被引 → 去重 → 关键词预筛 → 下一轮前沿
  → 饱和判断：某轮新增相关数 ≤ 阈值即停止，并写明停止理由
  → 合法开放全文分诊（Europe PMC OA 子集 + Unpaywall）
  → 导出 RIS / BibTeX / ASReview CSV / PRISMA 数字 / 饱和曲线
```

## 2. 记录如何进入人工筛选

| 来源 | 是否经过关键词预筛 | 理由 |
|---|---|---|
| PubMed、Europe PMC 布尔检索 | 否，全部进入人工筛选 | 检索式本身就是人工设计的过滤器 |
| OpenAlex、Crossref 排序检索 | 是 | 自由文本排序，命中数不代表主题召回 |
| 引文滚雪球候选 | 是 | 引文网络噪声大，需先粗筛再扩展 |

关键词预筛规则用 `--require` 指定，多组之间为 AND，组内同义词用 `|` 分隔，
末尾 `*` 表示词前缀。例如：

```bash
--require 'radiat*|irradiat*|radiotherap*|ionizing' \
--require 'muscle*|myopath*|myofib*|sarcopen*|satellite cell*'
```

预筛排除的记录不会丢弃，单独写入 `auto-excluded-v0.4.csv` 以便抽查。

## 3. 引文图与饱和

- 默认引文图：Europe PMC `references` / `citations`，无需密钥。它只覆盖 Europe PMC
  文本挖掘或出版商提供的引文关系，不是完整引文索引。
- 可选：`--graph openalex`（需要免费 `OPENALEX_API_KEY`）。
- 每篇论文的链接数上限 `--per-paper-limit`；超过上限的论文数在每轮记录中单独计数。
- 每轮记录：前沿大小、后向/前向链接数、无 PMID 链接数、新增唯一候选、新增相关、
  累计相关、被截断论文数、错误。
- 停止规则：某轮新增相关 ≤ `--stop-threshold`（默认 0）即判定“饱和”；
  达到 `--max-rounds` 仍有新增时明确写“未饱和”。

## 4. 合法开放全文

| 分类 | 含义 | 自动下载 |
|---|---|---|
| `oa_machine_readable` | 有 OA 位置且许可证在白名单（CC BY / CC BY-SA / CC0 / 公有领域） | 仅在 `--download-oa` 时，通过现有失败关闭下载器 |
| `oa_link_only` | 有 OA 位置但许可证缺失、NC/ND 或受限 | 否，只列链接供人工打开 |
| `needs_institutional_access` | 未找到合法 OA 位置 | 否；走机构权限、馆际互借或联系作者 |
| `no_identifier` | 无 DOI/PMCID | 否 |

Unpaywall 需要联系邮箱：通过环境变量 `UNPAYWALL_EMAIL` 或 `RESEARCH_AGENT_EMAIL`
提供；存档时邮箱和 `api_key` 会从 URL 中删除。本项目不接入任何影子图书馆，也不绕过
付费墙或登录。

## 5. 输出文件

```text
exhaustive-<UTC时间>-<查询词>/
├── exhaustive-manifest-v0.4.json   配置、各来源贡献、每轮记录、停止理由、问题、输出 SHA-256
├── exhaustive-report-v0.4.md       人读报告
├── screening-v0.4.ris / .bib       进入人工筛选的记录（Zotero 可直接导入）
├── asreview-v0.4.csv               ASReview LAB 输入；included 列留空
├── auto-excluded-v0.4.csv          被关键词预筛排除的记录
├── saturation-v0.4.csv / .svg      饱和曲线
├── prisma-v0.4.json                PRISMA 2020 流程图数字
├── oa-triage-v0.4.jsonl            每条记录的合法全文分诊
├── needs-institutional-access-v0.4.tsv
└── raw-responses/                  原始响应存档（已去除邮箱与密钥）
```

## 6. 已知局限

- Crossref 自由文本检索命中数以百万计，只取前 N 条，状态恒为 `truncated`。
- OpenAlex 列表接口无密钥时可能返回 HTTP 429；该情况记为来源问题，不当作“0 条结果”。
- 引文滚雪球只沿有 PMID 的链接扩展；无 PMID 的链接数被计数但未解析。
- 关键词预筛是透明的粗筛，会有假阳性和假阴性；它不替代人工题目/摘要筛选。
- Semantic Scholar 尚未接入（留待后续版本）。


## 机构权限档案（v0.4）

`needs-institutional-access` 清单可以按**你自己机构**的访问档案生成手动核对页 `institutional-access-v0.4.html`：每篇文献给出该出版平台的 CARSI/Shibboleth 联邦登录入口和 DOI 链接；没匹配到的走 WebVPN 或馆际互借。

```bash
cp profiles/institution-profile-example-v0.4.json ~/.config/research-agent/my-institution-v0.4.json  # 填入你所在图书馆公布的入口链接
export RESEARCH_AGENT_INSTITUTION_PROFILE=~/.config/research-agent/my-institution-v0.4.json
research-agent exhaustive '...' --require '...'            # 运行时自动套用
research-agent institution-links runs/exhaustive-...        # 或对已有运行重新生成
```

- 档案**不含任何账号、密码、Cookie 或令牌**，含这类字段会被拒绝；档案放在仓库外，路径不写入 manifest。
- 工具**不登录、不保存会话、不通过机构权限自动下载**。逐篇由有权限的本人打开（可配合 Zotero Connector）；图书馆许可普遍禁止用软件批量下载。
- 其他人使用本项目时，需要提供**自己订阅机构**的档案；仓库只附通用示例。
