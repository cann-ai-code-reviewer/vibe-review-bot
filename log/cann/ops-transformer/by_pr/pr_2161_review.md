# Code Review: PR #2161

| 属性 | 值 |
|------|------|
| 标题 | 新增拦截A5 PA场景不支持query dtype为int8 |
| 作者 | yangxh1203 |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2161](https://gitcode.com/cann/ops-transformer/merge_requests/2161) |
| 审查时间 | 2026-03-01 15:38:19 |
| 审查工具 | Claude Code (`codereview` skill) |
| 基线提交 | 97684e0e5a81 |

---

## 变更概述

本PR在PromptFlashAttention V2 tiling中新增了PA(PagedAttention)场景下query dtype为INT8的拦截检查,与V1 tiling(A3)的行为保持一致。同时对`CheckPerTensorQuantParams`函数中3处错误消息的首字母进行了大写修正。

变更内容：
- `SetAttributeInfo`的PA check块中新增`inputType == ge::DT_INT8`的拦截（+2行）
- `CheckPerTensorQuantParams`中3条错误消息的首个参数名首字母改为大写（3处文本替换）

调用顺序验证：`CheckIO`(line 4327)在`SetAttributeInfo`(line 4896)之前调用,`CheckIO`内部通过`CheckIODataType`初始化了`inputType = contextKeyParams.inputDataType`,因此新增检查处的`inputType`已正确初始化。

---

## 审查发现

### 发现 1：错误消息部分大写导致同一句内命名不一致

- 位置：`prompt_flash_attention_tiling_v2.cpp:996, 1002`
- 规则：错误消息一致性
- 置信度：高
- 问题代码：
```cpp
"DeqScale1, quantScale1 or deqScale2 is nullptr in per-tensor quant scenario."
```
```cpp
"DeqScale1, quantScale1 or deqScale2 is empty tensor in per-tensor quant scenario."
```
- 分析：同一条错误消息中列举了3个参数名,但只将第一个`deqScale1`首字母大写为`DeqScale1`,后面的`quantScale1`和`deqScale2`仍保持小写。这导致同一句内命名风格不一致。要么全部大写首字母,要么保持原样全部小写（因为它们是代码变量名/参数名,小写更贴近实际命名）。
- 修复建议：统一风格,建议三个参数名保持一致：
```cpp
"DeqScale1, QuantScale1 or DeqScale2 is nullptr in per-tensor quant scenario."
```
或还原为全部小写:
```cpp
"deqScale1, quantScale1 or deqScale2 is nullptr in per-tensor quant scenario."
```

### 发现 2：新增INT8拦截的错误消息与V1 tiling不一致

- 位置：`prompt_flash_attention_tiling_v2.cpp:4279`
- 规则：跨模块消息一致性
- 置信度：高
- 问题代码：
```cpp
"Query dataType can't be INT8 when PA enable."
```
- 分析：PR描述明确要求"应与A3保持一致"。V1 tiling(`prompt_flash_attention_tiling.cpp:3908`)中同功能的错误消息为`"Query DataType can't be INT8 when PA enable"`（`DataType`大写D大写T,无句末句号）。而V2新增的消息为`"Query dataType can't be INT8 when PA enable."`（小写d大写T,有句末句号），两处不一致。
- 修复建议：与V1保持一致：
```cpp
"Query DataType can't be INT8 when PA enable"
```

### 发现 3：新增检查的代码格式不符合上下文风格

- 位置：`prompt_flash_attention_tiling_v2.cpp:4279-4280`
- 规则：代码格式一致性
- 置信度：中
- 问题代码：
```cpp
        OP_CHECK_IF(inputType == ge::DT_INT8, OPS_REPORT_VECTOR_INNER_ERR(contextKeyParams.opName, "Query dataType can't be INT8 when PA enable."),
            return ge::GRAPH_FAILED);
```
- 分析：同一PA check块内的上方`blockSize`检查采用了三行展开格式（条件、报错信息、返回值各占一行或有适当换行），而新增的INT8检查将条件和报错信息压缩到同一行，导致单行过长且与相邻代码风格不统一。参考V1 tiling中的写法和上方`blockSize`检查的格式,应使用一致的多行展开。
- 修复建议：
```cpp
        OP_CHECK_IF(inputType == ge::DT_INT8,
            OPS_REPORT_VECTOR_INNER_ERR(contextKeyParams.opName,
            "Query DataType can't be INT8 when PA enable"),
            return ge::GRAPH_FAILED);
```

---

## 总结

核心逻辑正确：PA场景下拦截query dtype为INT8的检查位置合理,`inputType`在此处已被正确初始化,检查时机在`enablePA = true`之前。

主要问题集中在消息文本的一致性上：
1. 发现1和发现2属于同类问题——错误消息命名风格不统一。发现1是同一句内不一致,发现2是跨V1/V2的同功能消息不一致,与PR"应与A3保持一致"的目标矛盾。
2. 发现3是格式问题,建议对齐。

建议修复后合入。
