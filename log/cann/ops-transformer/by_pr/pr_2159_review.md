# Code Review: PR #2159

| 属性 | 值 |
|------|------|
| 标题 | alltoallvgmm 修复量化 模板转置问题 & aclnn 共享转置问题 |
| 作者 | libohao6 |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2159](https://gitcode.com/cann/ops-transformer/merge_requests/2159) |
| 审查时间 | 2026-03-01 16:28:00 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | ce448832ce94 |

---

## 变更概述

本MR修复alltoallv量化GMM算子的两个bug：
- `aclnn_allto_allv_quant_grouped_mat_mul.cpp`: 修复变量遮蔽bug——`auto mmWeightOptional`声明了新的局部变量，遮蔽了外层参数，导致转置后的tensor未传递给后续函数调用
- `allto_allv_grouped_mat_mul_apt.cpp`: 修复`QuantGroupedMatmul`模板参数错配——`aTrans`错误使用了权重转置标志(应为`false`)，ComputeOpType的`bTrans`错误使用了MM权重转置标志(应为GMM权重转置标志)

涉及2个文件，共3处修改。

## 审查发现

未发现问题。

两处修改均为正确的bug修复：

1. 变量遮蔽修复：已通过`git show`确认函数签名为`const aclTensor *mmWeightOptional`（参数为非const指针），赋值合法。修复后`mmWeightOptional`能正确传递给后续的`CheckParams`和`InnerAlltoAllvQuantGroupedMatMulGetWorkspaceSize`调用。同文件中`gmmWeight`的处理(`transposeGmmWeight = TransGmmWeightTensor(gmmWeight)`)采用相同模式，印证修复方向正确。缩进从8空格修正为12空格也符合内层`if`块的缩进层级。

2. 模板参数修复：已读取`quant_grouped_matmul.h:32-33`确认模板签名为`<..., CubeFormat wFormat, bool aTrans, bool bTrans, bool isLocal>`。修复后：
   - `aTrans`从`TILINGKEY_GMM_WEIGHT_TRANSPOSE`改为`false`——`aTrans`控制输入矩阵X的转置，X不应受权重转置标志影响
   - ComputeOpType(isLocal=false)的`bTrans`从`TILINGKEY_MM_WEIGHT_TRANSPOSE`改为`TILINGKEY_GMM_WEIGHT_TRANSPOSE`——非local计算处理GMM部分，应使用GMM权重转置标志
   - LocalComputeOpType(isLocal=true)的`bTrans`保持`TILINGKEY_MM_WEIGHT_TRANSPOSE`不变——local计算处理MM部分，使用MM权重转置标志

   已通过grep确认`QuantGroupedMatmul`仅在此文件实例化，无其他遗漏。

## 总结

本MR修复了两个实际bug，变更精准且正确。建议合入。
