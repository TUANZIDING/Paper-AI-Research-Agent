# Claim–Evidence 全文位置绑定

本模块只建立“某条 claim 指向已取得材料中的哪个确定位置”的可审计绑定。它不
判断研究结论是否真实，不把字符串命中解释为科学支持，也不执行论文、网页、
OCR 文本或事件日志中的任何指令。

## 两个相互独立的门

1. **定位与完整性门（软件可验证）**
   - 材料 bytes 必须匹配 `material_sha256`；
   - claim 原文保存为 `claim_text_hash`；
   - 正式绑定的定位文本必须由同一份已哈希材料在模块内部确定性提取；
   - 当前正式绑定仅支持 UTF-8 纯文本和 JATS 风格 article XML 的 quote/paragraph；
   - quote 必须在已取得文本中精确且唯一出现；
   - 定位所得 excerpt 生成 `excerpt_sha256`，可用预期哈希再次阻断错配。

2. **语义判断与人工状态门（软件不可推断）**
   - `SUPPORTED`、`UNSUPPORTED`、`AMBIGUOUS`、
     `UNVERIFIABLE_ACCESS` 是调用方提交的判断，不是字符串检索结果；
   - 没有受控事件时，定位成功仍为 `human_review_status=not_attested`；
   - 自动位置绑定无法证明 claim 的原子性或语义蕴含；因此任何请求的
     `SUPPORTED` 在外部受控语义裁决接入前都降为 `AMBIGUOUS`；
   - 人工状态只能引用已经存在、哈希链有效、材料哈希一致的
     `HUMAN_READ_SELF_ATTESTED` 事件；由于事件没有外部身份认证，结果恒为
     `self_attested` 且 `identity_verified=false`，不能写成 confirmed。
   - 若后续受控事件已撤销该核读事件，原事件哈希不得再用于绑定。

## Anchor 约定

- `page`: 定位器支持该形状，但正式绑定在受控分页提取产物实现前失败关闭；
- `paragraph`: `"2"` 或 `"2-4"`，段落由空行分隔；
- `section`: 定位器支持映射；正式绑定在映射派生链实现前失败关闭；
- `quote`: 与已取得全文文本逐字符精确匹配，并且只出现一次。

无法取得材料时可以记录语义状态 `UNVERIFIABLE_ACCESS`，但不能绕过材料哈希和
定位门生成一个虚假的 `located` 绑定。访问受限不等于论文不存在或 claim 为假。

## 边界

- OCR 错误、版式变化和不同论文版本会改变 excerpt hash，必须针对确切材料重做
  绑定；
- PDF/OCR、页码、section 和表图绑定尚未完成，不得用平行文本绕过材料派生门；
- Schema 约束字段形状，受控函数负责范围、唯一 quote、哈希和事件链语义；
- 本模块不下载全文、不绕过付费墙，也不创建人工身份、签名或独立专家批准。
