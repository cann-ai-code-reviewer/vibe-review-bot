# Code Review: PR #2161

| 属性 | 值 |
|------|------|
| 标题 | 新增拦截A5 PA场景不支持query dtype为int8 |
| 作者 | yangxh1203 |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2161](https://gitcode.com/cann/ops-transformer/merge_requests/2161) |
| 审查时间 | 2026-03-01 16:27:36 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | 4d2e750ad865 |
| 发现 | 严重0 / 一般1 / 建议2 |

---

## 变更概述

本MR为PromptFlashAttention算子新增PA场景下query dtype为INT8的拦截检查，使A5平台行为与A3保持一致。同时修改了3处错误消息的首字母大写。

- prompt_flash_attention_tiling_v2.cpp: 在`SetAttributeInfo`的PA check块中新增INT8拦截；修改3处错误消息字符串首字母大写

涉及1个文件，6行新增/3行修改。

## 审查发现

共发现3个问题（严重0 / 一般1 / 建议2）

---

### #1 [一般] 错误消息中参数名大小写不一致
- 位置：`attention/prompt_flash_attention/op_host/prompt_flash_attention_tiling_v2.cpp:996, 1002`
- 规则：1.3.x (注释/消息一致性)
- 置信度：确定

问题代码：
```cpp
"DeqScale1, quantScale1 or deqScale2 is nullptr in per-tensor quant scenario."),
```
```cpp
"DeqScale1, quantScale1 or deqScale2 is empty tensor in per-tensor quant scenario."),
```

同一消息中列举了三个参数名，但只对第一个`DeqScale1`做了首字母大写，`quantScale1`和`deqScale2`仍保持原样。这既不是句首大写（那只需要第一个词大写，而这些是参数名不是普通英文词），也不是统一大写参数名首字母（那三个都该改）。结果是同一字符串内风格自相矛盾，看上去像漏改。

修复建议：三个参数名保持统一风格。若目的是句首大写，则只改第一个即可（现状没问题）；若目的是参数名首字母统一大写，则三个都改：
```cpp
"DeqScale1, QuantScale1 or DeqScale2 is nullptr in per-tensor quant scenario."),
```
```cpp
"DeqScale1, QuantScale1 or DeqScale2 is empty tensor in per-tensor quant scenario."),
```

---

### #2 [建议] 错误消息与变量名不一致
- 位置：`attention/prompt_flash_attention/op_host/prompt_flash_attention_tiling_v2.cpp:984`
- 规则：1.3.x
- 置信度：确定（已通过Read确认变量名定义于第962行为`inputParamsType`）

问题代码：
```cpp
"InputParamsType must be INT8 in per-tensor quant scenario, now is %s",
```

第962行定义的变量名为`inputParamsType`（小写`i`），修改后错误消息变为`"InputParamsType"`（大写`I`）。原始消息`"inputParamsType"`与变量名完全一致，修改后反而产生了不匹配。开发者根据错误日志在代码中搜索`InputParamsType`将无法命中。

修复建议：保持消息中的名称与变量名一致，或改用更具描述性的文本：
```cpp
"inputParamsType must be INT8 in per-tensor quant scenario, now is %s",
```

---

### #3 [建议] 新增错误消息与相邻消息标点不一致
- 位置：`attention/prompt_flash_attention/op_host/prompt_flash_attention_tiling_v2.cpp:4280`
- 规则：1.3.x
- 置信度：确定

问题代码：
```cpp
"Query dataType can't be INT8 when PA enable."),
```

新增消息末尾有句点（`enable.`），而紧邻上方第4277行的消息无句点（`"blockSize can't be null when PA enable"`）。同一代码块内的错误消息标点风格应统一。

修复建议：与相邻消息保持一致，去掉句点：
```cpp
"Query dataType can't be INT8 when PA enable"),
```

---

## 总结

新增的INT8拦截逻辑本身正确——在PA check块中、`enablePA = true`之前拦截，检查字段`inputDataType`即query的dtype，与仓库中其他INT8检查（如第2114、4140行）使用同一字段，语义一致。
问题集中在错误消息文本的一致性上：同一消息内参数名大小写不统一（#1）、消息文本与变量名不匹配（#2）、标点风格不一致（#3）。建议在合入前统一处理。
