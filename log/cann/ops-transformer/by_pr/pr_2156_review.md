# Code Review: PR #2156

| 属性 | 值 |
|------|------|
| 标题 | modified the mxquant formula |
| 作者 | Kiana1216 |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2156](https://gitcode.com/cann/ops-transformer/merge_requests/2156) |
| 审查时间 | 2026-03-01 16:39:03 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | 8f89d5d9ca66 |
| 发现 | 严重0 / 一般2 / 建议2 |

---

## 变更概述

本PR为allto_all_matmul和matmul_allto_all两个MC2算子的文档进行了结构重组和mx量化公式修正，主要变更：

- mc2/allto_all_matmul/README.md: 将原先按场景组织（非量化/K-C量化/K-C动态量化）的公式段落重构为按平台组织（Atlas A2 / Ascend 950），并在Ascend 950下新增mx量化公式
- mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md: 同上结构重组，按平台拆分并新增Ascend 950 mx量化公式
- mc2/matmul_allto_all/README.md: 同上结构重组，新增Ascend 950下非量化、K-C量化和mx量化公式
- mc2/matmul_allto_all/docs/aclnnQuantMatmulAlltoAll.md: 同上结构重组，新增Ascend 950下mx量化公式

涉及4个文档文件，核心变更是将mx量化的计算公式从原先与K-C量化共用的dequant-after-matmul形式 `output = (x1 @ x2) × scale`，修正为Ascend 950平台的scale-before-matmul形式 `output = (x1 * x1Scale) @ (x2 * x2Scale) + bias`。

## 审查发现

共发现4个问题（严重0 / 一般2 / 建议2）

---

### #1 [一般] Atlas A2平台缺少mx量化场景公式

- 位置：`mc2/allto_all_matmul/README.md:19-47`, `mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md:19-40`, `mc2/matmul_allto_all/README.md:19-34`, `mc2/matmul_allto_all/docs/aclnnQuantMatmulAlltoAll.md:19-27`
- 规则：文档完整性
- 置信度：较确定——已确认四个文件的接口描述（line 16）均声明支持mx量化，但Atlas A2公式段落中均不包含mx量化场景

问题代码（以allto_all_matmul/README.md为例）：
```markdown
- 算子功能：...支持非量化、K-C量化、K-C动态量化和mx量化模式。  ← line 16, 声明支持mx

    - <term>Atlas A2 训练系列产品/Atlas A2 推理系列产品</term>：
      - 非量化场景：  ← line 20
      - K-C量化场景：  ← line 28
      - K-C动态量化场景：  ← line 38
      ← 缺少mx量化场景
```

分析：PR将原先K-C量化与mx量化共用的公式拆分后，仅在Ascend 950下新增了独立的mx量化公式。但四个文件的接口描述行（line 16）均声称支持mx量化，没有平台限定。这导致用户查看Atlas A2平台的公式时找不到mx量化的计算公式，产生文档与声明的不一致。

修复建议：
- 如果Atlas A2确实支持mx量化，需在Atlas A2段落下补充对应的mx量化公式
- 如果Atlas A2不支持mx量化，需修改接口描述行，明确mx量化仅限特定平台（例如将"支持...mx量化模式"移到平台限定说明下）

---

### #2 [一般] allto_all_matmul的Ascend 950平台缺少K-C量化场景公式

- 位置：`mc2/allto_all_matmul/README.md:49-74`, `mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md:42-60`
- 规则：文档完整性
- 置信度：较确定——已确认allto_all_matmul接口描述声明支持K-C量化，且对比matmul_allto_all（Ascend 950下有K-C量化，见mc2/matmul_allto_all/README.md:45），allto_all_matmul的Ascend 950缺少该场景

问题代码（mc2/allto_all_matmul/README.md）：
```markdown
    - <term>Ascend 950PR/Ascend 950DT</term>：
      - 非量化场景：          ← line 50
      - K-C动态量化场景：     ← line 58
      - mx量化场景：          ← line 67
      ← 缺少K-C量化场景
```

问题代码（mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md）：
```markdown
  - <term>Ascend 950PR/Ascend 950DT</term>：
    - K-C动态量化场景：       ← line 44
    - mx量化场景：            ← line 53
    ← 缺少K-C量化场景
```

分析：allto_all_matmul README.md line 16声明支持K-C量化，且Atlas A2段落已包含K-C量化公式。但Ascend 950段落缺少K-C量化场景。如果Ascend 950的K-C量化公式与Atlas A2相同，也应列出（因为公式段落已按平台拆分）；如果不同（参考matmul_allto_all中两平台的K-C量化公式有差异：Atlas A2是`(x1@x2)*scale+bias`，Ascend 950是`(x1@x2+bias)*scale`），则更需要补充Ascend 950特有的公式。

修复建议：在Ascend 950段落下补充K-C量化场景公式。可参考matmul_allto_all中Ascend 950 K-C量化的公式风格（dequant位置可能不同于Atlas A2）。

---

### #3 [建议] K-C量化公式定义了permutedOut但未使用

- 位置：`mc2/allto_all_matmul/README.md:33`, `mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md:26`
- 规则：文档正确性
- 置信度：较确定——已对比同文件中非量化公式（line 25使用`permutedOut @ x2`）和K-C动态量化公式（line 43-44通过`Quant(permutedOut)`间接使用），仅K-C量化公式使用`x1`而非`permutedOut`

问题代码（mc2/allto_all_matmul/README.md:31-33）：
```latex
permutedOut = commOut.permute(1, 0, 2).view(BS/rankSize, rankSize*H) \\
output_{quant} = x1 @ x2 \\
```

分析：公式先对AlltoAll输出做permute得到`permutedOut`，但matmul步骤使用的是原始输入`x1`而非`permutedOut`。同文件内其他场景均正确使用`permutedOut`（非量化：`permutedOut @ x2`；K-C动态量化：`Quant(permutedOut)` → `x1_{quant} @ x2`）。此问题在PR之前即存在，但本PR重构公式段落时可一并修正。

修复建议：
```latex
permutedOut = commOut.permute(1, 0, 2).view(BS/rankSize, rankSize*H) \\
output_{quant} = permutedOut @ x2 \\
```

---

### #4 [建议] mx量化公式中乘法符号与其他公式风格不统一

- 位置：`mc2/allto_all_matmul/README.md:73`, `mc2/allto_all_matmul/docs/aclnnAlltoAllQuantMatmul.md:59`, `mc2/matmul_allto_all/README.md:56`, `mc2/matmul_allto_all/docs/aclnnQuantMatmulAlltoAll.md:42`
- 规则：文档风格一致性
- 置信度：确定

问题代码（以allto_all_matmul/README.md:73为例）：
```latex
output = (permutedOut* commx1Scale)@(x2* x2Scale) + bias
```

分析：所有新增的mx量化公式使用`*`（LaTeX中渲染为星号 \*）表示标量乘法，而同文件中其他公式统一使用`\times`（渲染为 ×）。例如K-C动态量化公式（line 64）使用`\times dynQuantX1Scale \times x2Scale`。此外`*`前缺少空格（如`permutedOut*`），降低了可读性。

修复建议：
```latex
output = (permutedOut \times commx1Scale)@(x2 \times x2Scale) + bias
```

---

## 总结

本PR的核心意图（修正mx量化公式为scale-before-matmul形式）方向正确，但文档重构后存在量化场景覆盖缺失的问题：Atlas A2下四个文件均未列出mx量化公式，allto_all_matmul中Ascend 950又缺少K-C量化公式。这些遗漏会导致用户在查阅特定平台文档时找不到对应的计算公式。建议优先处理2个一般问题，确认各平台实际支持的量化模式后补全公式段落。
