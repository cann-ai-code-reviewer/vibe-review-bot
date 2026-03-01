# Code Review: PR #2151

| 属性 | 值 |
|------|------|
| 标题 | ring_attention算子文档更新 |
| 作者 | mirror-center |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2151](https://gitcode.com/cann/ops-transformer/merge_requests/2151) |
| 审查时间 | 2026-03-01 16:33:43 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | 90d8f665a892 |

---

## 变更概述

本PR将 `aclnnRingAttentionUpdate` 和 `aclnnRingAttentionUpdateV2` 两个算子文档中的参数说明部分，从 markdown 列表格式重构为 HTML 表格格式。变更涉及2个 `.md` 文件，无代码变更。表格新增了"维度(shape)"列，将原本散落在描述中的 shape 信息集中展示。同时将产品限制说明（Atlas A2/A3 的 D 限制）从各参数下方的子项移至表格外的统一注释。

---

## 审查发现

### 发现 1: HTML table style 属性包含无效值 `undefined;`

- 位置: `attention/ring_attention_update/docs/aclnnRingAttentionUpdate.md:65`, `attention/ring_attention_update/docs/aclnnRingAttentionUpdateV2.md:64`
- 规则: 文档正确性
- 置信度: 高

问题代码:
```html
<table style="undefined;table-layout: fixed; width: 1565px">
```

分析: `style` 属性中以 `undefined;` 开头，这是典型的 JavaScript 变量未赋值导致的模板渲染错误。虽然浏览器会忽略无法解析的 CSS 声明，但这属于明显的残留 bug，暗示表格可能由工具生成且生成过程存在问题。两个文件中的表格都有此问题。

修复建议:
```html
<table style="table-layout: fixed; width: 1565px">
```

---

### 发现 2: 维度缩写含义说明被删除

- 位置: `attention/ring_attention_update/docs/aclnnRingAttentionUpdate.md:95-103`, `attention/ring_attention_update/docs/aclnnRingAttentionUpdateV2.md:84-92`
- 规则: 信息完整性
- 置信度: 高

问题代码（原文被删除的内容）:
```
此处B为batch size，N为head number，S为sequence length，T为time。
```

分析: 原文档在 `prevSoftmaxMax` 参数描述中说明了 B、N、S、T 各维度缩写的含义。重构为表格后，shape 列直接使用 `[B,N,S,8]`、`[T,N,D]`、`[S,B,H]` 等缩写，但未在任何地方解释这些字母的含义。对于首次阅读文档的用户，缺少这些定义会造成理解障碍。

修复建议: 在表格上方或下方添加维度缩写说明，例如：
```
其中B为batch size，N为head number，S为sequence length，T为time，D为head dimension，H为hidden size。
```

---

### 发现 3: 参考文档超链接被移除

- 位置: `attention/ring_attention_update/docs/aclnnRingAttentionUpdate.md:76-244`, `attention/ring_attention_update/docs/aclnnRingAttentionUpdateV2.md:75-255`
- 规则: 信息完整性
- 置信度: 高

问题代码（以 prevAttnOut 为例，原文链接被删除后在表格中仅用 `√` 和 `ND` 替代）:
```html
<td>ND</td>
...
<td>√</td>
```

分析: 原文档中每个 tensor 参数都包含指向 `非连续的Tensor.md` 和 `数据格式.md` 的超链接：
```markdown
支持[非连续的Tensor](../../../docs/zh/context/非连续的Tensor.md)，[数据格式](../../../docs/zh/context/数据格式.md)支持ND
```
重构后这些链接全部丢失。对于需要了解"非连续Tensor"具体含义或"ND数据格式"定义的用户，失去了直接跳转的路径。

修复建议: 在表头或表格下方的注释中补充链接，例如在表格下方添加：
```markdown
- 数据格式说明参见[数据格式](../../../docs/zh/context/数据格式.md)
- 非连续Tensor说明参见[非连续的Tensor](../../../docs/zh/context/非连续的Tensor.md)
```
或者在表头单元格中直接嵌入链接：
```html
<th><a href="../../../docs/zh/context/数据格式.md">数据格式</a></th>
<th><a href="../../../docs/zh/context/非连续的Tensor.md">非连续Tensor</a></th>
```

---

### 发现 4: 固定像素宽度可能导致渲染问题

- 位置: `attention/ring_attention_update/docs/aclnnRingAttentionUpdate.md:65-73`, `attention/ring_attention_update/docs/aclnnRingAttentionUpdateV2.md:64-72`
- 规则: 文档可用性
- 置信度: 中

问题代码:
```html
<table style="undefined;table-layout: fixed; width: 1565px">
    <colgroup>
        <col style="width: 146px">
        <col style="width: 135px">
        <col style="width: 326px">
        <col style="width: 246px">
        <col style="width: 275px">
        <col style="width: 101px">
        <col style="width: 190px">
        <col style="width: 146px">
    </colgroup>
```

分析: 表格总宽度固定为 1565px，各列宽度也使用固定像素值。在窄屏设备、GitCode 的 MR diff 视图、或某些 markdown 渲染器中，这个宽度可能超出可视区域导致水平滚动。此外，`table-layout: fixed` 配合固定列宽会阻止列根据内容自适应。建议使用百分比宽度或移除固定宽度限制，让表格自适应容器。

修复建议: 使用相对宽度或移除固定约束：
```html
<table style="table-layout: auto; width: 100%">
```
并删除 `<colgroup>` 中的固定像素宽度，或改为百分比。

---

## 总结

本PR将参数说明从列表改为表格，结构上更清晰，但在格式转换过程中存在几个需要关注的问题：

1. `style="undefined;..."` 是明显的模板渲染残留，应修复（发现1）
2. 维度缩写含义（B/N/S/T/D/H）的解释被完全删除，影响文档自包含性（发现2）
3. 原文中指向"非连续Tensor"和"数据格式"的参考链接全部丢失（发现3）
4. 固定像素宽度在不同渲染环境下可能产生布局问题（发现4）

建议优先修复发现1和发现2，同时考虑补充发现3中的参考链接。
