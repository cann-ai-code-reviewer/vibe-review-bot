# Code Review: PR #2154

| 属性 | 值 |
|------|------|
| 标题 | feature:G泛化放开D约束 |
| 作者 | zuoyw |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2154](https://gitcode.com/cann/ops-transformer/merge_requests/2154) |
| 审查时间 | 2026-03-01 16:30:40 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | 6e95e5720c5f |

---

## 变更概述

本MR为Flash Attention算子实现G泛化放开D约束，主要变更：
- flash_attention_score_block_cube.h: 在三处Q tensor搬运逻辑中，对非NTD layout使用`runInfo.queryOffset`替代`offsetCalculator.GetOffset`计算的`gmOffset`，以支持TND等layout下G值不受D约束限制
- prompt_flash_attention_tiling_v2.cpp: 删除GQA非量化场景下D不为64/128时G<=64的约束检查，将`else if (enableIFAMLA...)`改为`else`

涉及2个文件，3处kernel侧新增layout分支 + 1处host侧约束删除。

## 审查发现

共发现3个问题（一般2 / 建议1）

---

### #1 [一般] 续行参数缩进未对齐

- 位置：`attention/common/op_kernel/arch35/flash_attention_score_block_cube.h:1103`
- 规则：1.2（代码缩进/对齐）
- 置信度：确定

问题代码：
```cpp
                 constInfo.mm1Ka); 
```

在`IterateBmm1DnSplitK`的非NTD分支中，`CopyToL1Nd2Nz`的续行参数`constInfo.mm1Ka`缩进为17个空格，而上方NTD分支中相同位置的续行缩进为20个空格。同一个`if-else`块内续行风格应保持一致。

修复建议：
```cpp
                    constInfo.mm1Ka); 
```

---

### #2 [一般] 新增代码行尾存在trailing whitespace（tab+空格）

- 位置：`attention/common/op_kernel/arch35/flash_attention_score_block_cube.h:913, 915-917, 1098-1100, 1636-1638`
- 规则：1.2（代码格式）
- 置信度：确定

问题代码（以第一处为例）：
```cpp
                if constexpr (layout == LayOutTypeEnum::LAYOUT_NTD) {	 
```

三处修改中，NTD分支的代码行尾均存在tab+空格的trailing whitespace（通过`cat -e`确认行尾有`\t $`或`\t  $`）。非NTD分支的行尾也有trailing空格。这些在代码审查工具和CI检查中通常会被标记。

修复建议：删除所有新增行的行尾空白字符。

---

### #3 [建议] 删除D约束校验后缺少PR描述说明

- 位置：`attention/prompt_flash_attention/op_host/prompt_flash_attention_tiling_v2.cpp:887-893`（已删除行）
- 规则：2.2（外部输入校验）
- 置信度：待确认

问题代码（被删除）：
```cpp
        if ((nQ / nKV > GLIMIT_64 || nQ > NLIMIT) && CHECK_D_LIMITED_SCENARIO(queryShapeInfo.d)) {
            OP_LOGE(contextKeyParams.opName, "In gqa non quant scenario, when dSize is not 64 or 128, the G(numHeads / numKeyValueHeads) "
                "connot be larger than %d or the numHeads cannot be larger than %d, but numHeads = %d, numKeyValueHeads = %d.",
                GLIMIT_64, NLIMIT, nQ, nKV); 
            return false; 
        } 
```

此PR删除了GQA非量化场景下"D不为64/128时G不能超过64"的约束。kernel侧对应新增了非NTD layout使用`queryOffset`的分支来支撑这一放开。但以下问题需要人工确认：

1. `CHECK_D_LIMITED_SCENARIO`宏在该文件的2729和2768行仍有其他引用（可能是其他场景的约束），删除此处不影响那些引用，但需确认那些场景是否也需要同步放开。
2. PR描述为空白模板，未说明放开D约束的业务背景、测试覆盖情况，以及是否有对应的算子泛化测试用例覆盖新开放的G/D组合。建议补充PR描述。

修复建议：在PR描述中说明：(1) 放开约束的业务需求背景；(2) 对哪些D值和G值组合进行了测试验证；(3) 是否更新了算子规格文档。

---

## 总结

kernel侧三处修改逻辑正确，layout分支模式一致，`queryOffset`字段在所有场景下都有正确赋值。主要问题是代码格式（trailing whitespace和缩进不对齐），以及删除host侧约束时PR描述缺失。建议优先处理2个一般问题中的格式问题。
