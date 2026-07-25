# Crossmark 补充覆盖政策

## 当前合法、公开的数据入口

Crossref 官方说明，Crossmark 相关元数据可由任何人通过 Crossref 公共 REST API
访问；公共 REST API 无需注册或登录，并包含出版后更新、关系、更新政策和断言等
元数据：

- Crossmark 官方说明：<https://www.crossref.org/documentation/crossmark/>
- Crossref REST API：<https://www.crossref.org/documentation/retrieve-metadata/rest-api/>
- REST API 访问与认证：<https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/>
- REST filters（`updates:{doi}`、`update-type`）：<https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/>
- Crossmark 参与及 12 种官方 update 类型：<https://www.crossref.org/documentation/crossmark/participating-in-crossmark/>

Crossmark 面向读者的对话页示例为
`https://crossmark.crossref.org/dialog?doi=...`。官方文档将其描述为按钮/弹窗服务，
并未为本项目确认一个独立、稳定、结构化的机器 API 契约。因此 v0.3 后续适配层：

1. 不抓取或解析 Crossmark dialog HTML；
2. 不访问出版商登录页、订阅全文或付费墙；
3. 使用 Crossref 公共 REST work metadata 与 `filter=updates:{doi}` 作为公开 fallback；
4. 可接受调用方配置的规范化 provider，但不把该 provider 自动宣称为 Crossref
   官方 Crossmark 实时 API；
5. `crossmark_realtime_verified` 在当前实现中恒为 `false`。

## 适配层的保守语义

`src/ai_research_agent/crossmark.py` 只识别受控状态：

- `retracted`
- `correction`
- `expression_of_concern`
- `unknown`

优先级为撤稿、关注表达式、更正、未知。标题、摘要、断言、网页文本和 provider
返回的命令式内容不会升级为状态信号。

每次成功评估必须满足：

- DOI 格式有效；
- HTTP 响应为 2xx，JSON 为对象且不超过 5 MB；
- Crossref fallback 同时取得单篇 work 与反向 updates 响应；
- work DOI 与目标严格一致；
- 每个 update item 至少有一个 `update-to.DOI` 与目标一致；
- 混合目标记录只解析与目标 DOI 一致的子关系；
- `total-results` 与实际取得的 items 数量一致，避免截断后虚报完整；
- 记录响应 SHA-256、时间、URL、provider、目标 DOI 和稳定 evidence ID。

配置 provider 不可用、非 2xx、响应恶意、结构不明或无法证明目标绑定时均
fail closed。若随后使用 Crossref fallback，结果会明确：

- `route=crossref_public_rest_fallback`
- `fallback_used=true`
- `crossmark_realtime_verified=false`
- `errors` 保留主 provider 失败原因

## 不能由该结果推出的结论

- `unknown` 不等于已排除撤稿、更正或关注表达式；
- `crossmark_participation_declared=true` 只表示 work metadata 含更新政策，
  不保证出版商已经完整、及时登记所有更新；
- Crossref fallback 完成不等于独立 Crossmark dialog 实时核验完成；
- 本适配层不验证全文当前版本、不评价研究质量，也不替代 PubMed、Retraction
  Watch、Crossmark/Crossref 之外的来源或 Embase、Scopus、Web of Science；
- 该模块已接入独立的 `check-crossmark` CLI，但尚未进入 `search` pipeline；
  不能从普通检索报告推断它已被自动执行。
