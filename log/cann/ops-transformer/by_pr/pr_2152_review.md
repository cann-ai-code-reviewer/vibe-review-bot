# Code Review: PR #2152

| 属性 | 值 |
|------|------|
| 标题 | 确定性模版支持BSH格式 |
| 作者 | lairuhao |
| 链接 | [https://gitcode.com/cann/ops-transformer/merge_requests/2152](https://gitcode.com/cann/ops-transformer/merge_requests/2152) |
| 审查时间 | 2026-03-01 16:41:34 |
| 审查工具 | Claude Code (`vibe-review` skill) |
| 基线提交 | 7eb74e902a1c |
| 发现 | 严重8 / 一般4 / 建议2 |

---

## 变更概述

本MR为FlashAttentionScoreGrad的确定性模板(BasicDet)添加BSH格式支持，主要变更：
- `aclnn_flash_attention_score_grad.cpp`: 注释掉5处Unpadding接口中TND-only的layout检查
- `flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp`: SetBaseInfo新增BSH分支解析shape，DoOpTiling新增BSH的size计算和layout设置，去除IsCapable中TND限制，注释掉IsShapeCapable所有检查，注册优先级从1002改为100
- `addr_compute_det.h`: 新增layout成员和BSH格式的地址计算逻辑（UpdateSeqLen/getSeqLen/getTotalLen/getLeftAddr/getRightAddr）
- `vec_sfmg_det.h`: 新增BSH格式的softmax grad索引和数据拷贝逻辑
- `vec_op_det.h`: 新增BSH格式的序列长度获取和sfmg/softmax偏移计算
- `flash_attention_score_grad_s1s2_bn2gs1s2_basic_det.h`: 新增InputLayout enum和inputLayout成员
- `flash_attention_score_grad_tiling.h`: 新增s1/s2/layout字段及getter/setter

涉及7个文件，约344行新增/修改。

## 审查发现

共发现14个问题（严重8 / 一般4 / 建议2）

---

### #1 [严重] enum缺少分号导致编译错误
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/flash_attention_score_grad_s1s2_bn2gs1s2_basic_det.h:84-90`
- 规则：语法错误
- 置信度：确定

问题代码：
```cpp
enum InputLayout{
    BSH = 0,
    SBH = 1,
    BNSD = 2,
    BSND = 3,
    TND
}
```

enum定义末尾缺少分号，会导致编译失败。

修复建议：
```cpp
enum InputLayout{
    BSH = 0,
    SBH = 1,
    BNSD = 2,
    BSND = 3,
    TND
};
```

---

### #2 [严重] InputLayout重复定义，与已有的enum class冲突
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/flash_attention_score_grad_s1s2_bn2gs1s2_basic_det.h:84-90`
- 规则：2.1.3（冗余代码）
- 置信度：较确定 — 已确认`flash_attention_score_grad_tiling_common.h:73`中已定义`enum class InputLayout`，host tiling代码使用的是该定义

问题代码：
```cpp
enum InputLayout{
```

kernel header定义了一个非class的`enum InputLayout`，而`flash_attention_score_grad_tiling_common.h:73`中已存在`enum class InputLayout`。两者枚举值相同但类型不同（`enum` vs `enum class`），且kernel侧的enum定义在类作用域外，枚举值会污染全局命名空间。

修复建议：
删除kernel header中的重复enum定义，改为直接使用tiling data中传递过来的`uint32_t layout`值（当前代码已经这么做了），或将common header中的enum class移到一个kernel/host共享的位置。

---

### #3 [严重] DoOpTiling未调用set_s1()/set_s2()，内核侧读取到0值
- 位置：`attention/flash_attention_score_grad/op_host/arch32/flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:323-375`
- 规则：红线1.4（变量使用前必须有效初始化）
- 置信度：确定 — 已确认整个DoOpTiling()中无set_s1/set_s2调用；grep全文件无匹配；内核通过`tilingData->basicDetTensorTilingData.s1`和`.s2`读取

问题代码：
```cpp
tilingData->basicDetTensorTilingData.set_layout(static_cast<uint32_t>(InputLayout::BSH));
```

tiling.h中新增了`s1`和`s2`字段（初始值为0），并添加了`set_s1()`/`set_s2()` setter，但`DoOpTiling()`中从未调用这两个setter。内核侧多处依赖`tilingData->basicDetTensorTilingData.s1`和`.s2`来初始化BSH地址计算所需的`dimS1Fixed`和`dimS2Fixed`（例如`addr_compute_det.h:90-91`、`vec_op_det.h:175-176`），实际读到的值为0，导致BSH模式下所有地址计算产生错误结果。

修复建议：在DoOpTiling中设置layout之前或之后添加：
```cpp
tilingData->basicDetTensorTilingData.set_s1(fBaseParams.s1);
tilingData->basicDetTensorTilingData.set_s2(fBaseParams.s2);
```

---

### #4 [严重] getSeqLen() BSH路径始终返回dimS1Fixed，K序列长度错误
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/basic_modules/addr_compute_det.h:180-181`
- 规则：逻辑错误
- 置信度：确定 — 已确认line 99处`getSeqLen(i, seqLenK)`调用传入K序列数组，但BSH分支忽略参数始终返回Q长度

问题代码：
```cpp
if (layout == 0) {  // BSH格式：返回固定长度
    return dimS1Fixed;  // 对于Q，返回固定长度
}
```

`getSeqLen`被同时用于Q和K序列（line 99: `getSeqLen(i, seqLenK)`，line 103: `getSeqLen(i, seqLenQ)` vs `getSeqLen(i, seqLenK)`），但BSH分支始终返回`dimS1Fixed`（Q的长度）。当`s1 != s2`时，`maxSeqK`和`eqSeq`的计算结果都是错误的。

修复建议：区分Q和K返回不同的固定长度，例如通过参数判断或拆分为两个函数：
```cpp
if (layout == 0) {
    if (seq_Len == seqLenQ) return dimS1Fixed;
    return dimS2Fixed;
}
```
或者更清晰的方式是在BSH路径直接使用dimS1Fixed/dimS2Fixed，不走getSeqLen。

---

### #5 [严重] getTotalLen() BSH路径始终基于dimS1Fixed，K累积长度错误
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/basic_modules/addr_compute_det.h:194-195`
- 规则：逻辑错误
- 置信度：确定

问题代码：
```cpp
if (layout == 0) {  // BSH格式：返回基于固定长度的累积
    return (i + 1) * dimS1Fixed;  // 对于Q
}
```

与#4相同的问题。`getTotalLen`在TND路径中被`seqLenQ`和`seqLenK`两种参数调用（line 170-171），但BSH分支始终基于`dimS1Fixed`计算，对K序列应使用`dimS2Fixed`。

修复建议：同#4，区分Q和K序列。

---

### #6 [严重] sfmgOffset BSH路径缺少batchIdx乘数
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/basic_modules/vec_op_det.h:568-569`
- 规则：逻辑错误
- 置信度：确定 — 对比TND路径（line 572）使用累积序列长度`actual_seq_qlen_addr[batchIdx-1]`（等价于`batchIdx * dimS1Fixed`），BSH路径缺少`batchIdx`因子

问题代码：
```cpp
if (layout == 0){
    sfmgOffset = dimS1Fixed * n2 * g * 8;  
}
```

TND路径中`actual_seq_qlen_addr[batchIdx-1]`表示前batchIdx个batch的累积Q序列总长度。对于BSH固定长度格式，等价值应为`batchIdx * dimS1Fixed`，而非`dimS1Fixed`。当batchIdx > 1时，sfmg的地址偏移计算错误。

修复建议：
```cpp
if (layout == 0){
    sfmgOffset = batchIdx * dimS1Fixed * n2 * g * 8;
}
```

---

### #7 [严重] softMaxOffset BSH路径多处缺少batch维度乘数
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/basic_modules/vec_op_det.h:583-589`
- 规则：逻辑错误
- 置信度：确定 — 对比TND路径（line 593-599），`actual_seq_qlen_addr[batchIdx-1]`和`actual_seq_qlen_addr[batch-1]`分别等价于`batchIdx * dimS1Fixed`和`batch * dimS1Fixed`

问题代码：
```cpp
innerRowOffsetLeft =
    unlikely(batchIdx == 0) ?
        0 :
        dimS1Fixed * 32 / sizeof(float);

softMaxOffset = ((dimS1Fixed * 32 / sizeof(float)) *
                    (blockInfo.n2Idx * g + blockInfo.gIdx) +
                innerRowOffsetLeft + originInnerBatchOffset % (actualS1Len * 32 / sizeof(float)));
```

两处问题：(1) `innerRowOffsetLeft`应为`batchIdx * dimS1Fixed * 32 / sizeof(float)`；(2) `softMaxOffset`的首项应为`batch * dimS1Fixed * 32 / sizeof(float)`。

修复建议：
```cpp
innerRowOffsetLeft =
    unlikely(batchIdx == 0) ?
        0 :
        batchIdx * dimS1Fixed * 32 / sizeof(float);

softMaxOffset = ((batch * dimS1Fixed * 32 / sizeof(float)) *
                    (blockInfo.n2Idx * g + blockInfo.gIdx) +
                innerRowOffsetLeft + originInnerBatchOffset % (actualS1Len * 32 / sizeof(float)));
```

---

### #8 [严重] BSH路径headNum作为除数未做零值校验
- 位置：`attention/flash_attention_score_grad/op_host/arch32/flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:104`
- 规则：红线1.1（除法操作必须进行除零保护）
- 置信度：确定

问题代码：
```cpp
fBaseParams.d = queryShape->GetStorageShape().GetDim(DIM_2) / headNum; // H=N*D
```

`headNum`从属性`HEAD_NUM`获取（line 89），但在用作除数前未检查是否为0。TND路径不使用headNum做除法，这是BSH新引入的除零风险。

修复建议：在使用headNum做除法前添加校验：
```cpp
OP_CHECK_IF(headNum == 0, OP_LOGE(context_, "headNum is 0"), return ge::GRAPH_FAILED);
```

---

### #9 [一般] IsShapeCapable()中shape校验被完全注释掉
- 位置：`attention/flash_attention_score_grad/op_host/arch32/flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:316-318`
- 规则：TOPN 2.2（外部输入需校验合法性）
- 置信度：确定

问题代码：
```cpp
// if (fBaseParams.d != fBaseParams.dv || fBaseParams.d > SPECIAL_HEADDIM_128 || fBaseParams.d % C0_SIZE != 0) {
//     return false;
// }
return true;
```

原有的d/dv一致性检查、headDim上限检查（<=128）和C0对齐检查全部被注释掉。如果BSH确实需要放宽这些限制，应当明确注释原因并针对BSH单独处理，而非对所有format都去掉校验。此外BSH路径未初始化`fBaseParams.dv`，如果恢复校验会导致d!=dv误判。

修复建议：BSH分支应初始化dv，并根据实际支持的shape范围做针对性校验，而非完全跳过。

---

### #10 [一般] 生产代码中残留大量调试输出
- 位置：`flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:107-113, 374`, `addr_compute_det.h:92`, `vec_op_det.h:175, 177`, `vec_sfmg_det.h:163-164, 193, 201`
- 规则：2.1.3（冗余代码）
- 置信度：确定

问题代码：
```cpp
std::cout << "fBaseParams.b " << fBaseParams.b  << std::endl;
```

```cpp
AscendC::PRINTF("dimS1Fixed:%d, dimS2Fixed:%d", this->dimS1Fixed, this->dimS2Fixed);
```

共计15处`std::cout`和`AscendC::PRINTF`调试输出。host侧`std::cout`会在tiling阶段污染标准输出；kernel侧`AscendC::PRINTF`在NPU运行时有性能开销。

修复建议：删除所有调试输出，必要的日志使用`OP_LOGI`/`OP_LOGD`。

---

### #11 [一般] 注释掉代码而非正确移除
- 位置：`aclnn_flash_attention_score_grad.cpp:1517-1520, 1778-1781, 1961-1964, 2055-2058, 2351-2354`, `flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:85-88`
- 规则：2.1.3（冗余代码）
- 置信度：确定

问题代码：
```cpp
// if (strcmp(inputLayout, "TND") != 0) {
//     OP_LOGE(ACLNN_ERR_PARAM_INVALID, "layout %s is not TND, invalid shape, pls check", inputLayout);
//     return ACLNN_ERR_PARAM_INVALID;
// }
```

5处API入口和1处tiling中的TND-only检查都是注释掉而非删除或替换为正确的校验逻辑。注释掉的校验意味着Unpadding接口不再验证layout合法性，任何非TND/非BSH的layout也能通过。应替换为支持的layout白名单校验。

修复建议：删除注释代码，替换为明确的layout白名单校验：
```cpp
if (strcmp(inputLayout, "TND") != 0 && strcmp(inputLayout, "BSH") != 0) {
    OP_LOGE(ACLNN_ERR_PARAM_INVALID, "layout %s is not supported, pls check", inputLayout);
    return ACLNN_ERR_PARAM_INVALID;
}
```

---

### #12 [一般] 注册优先级从1002改为100，可能影响其他tiling模板选择
- 位置：`attention/flash_attention_score_grad/op_host/arch32/flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:530`
- 规则：TOPN 2.4（已有tiling_id语义不能发生变化）
- 置信度：待确认 — 不确定优先级数字含义（越小越优先还是越大越优先），但10倍的变化幅度需要确认是否有副作用

问题代码：
```cpp
    100);
```

原优先级1002改为100。如果数字越小优先级越高，此变更会使BasicDet模板在所有能力匹配的场景下优先于其他模板被选中，可能导致非确定性场景或非BSH场景的回退行为变化。

修复建议：确认优先级语义，如果是为了让BSH走此模板，应在IsCapable中正确控制，而非通过改优先级实现。

---

### #13 [建议] s1/s2 getter/setter类型与字段类型不匹配
- 位置：`attention/flash_attention_score_grad/op_kernel/arch32/flash_attention_score_grad_tiling.h:4694-4710`
- 规则：类型一致性
- 置信度：确定

问题代码：
```cpp
uint64_t get_s1() const { return s1; }
void set_s1(uint64_t s1_val) { this->s1 = s1_val; }
```

字段`s1`和`s2`声明为`int64_t`，但getter返回`uint64_t`、setter参数为`uint64_t`。其他同类字段（如同文件中line 85的`set_s1(int64_t s1_val)`）使用`int64_t`。

修复建议：将getter/setter的类型改为`int64_t`以保持一致。

---

### #14 [建议] BSH路径未初始化sumS1S2Product和actualSeqQlen/Kvlen
- 位置：`attention/flash_attention_score_grad/op_host/arch32/flash_attention_score_grad_tiling_s1s2_bn2gs1s2_basic_det.cpp:96-114`
- 规则：红线1.4
- 置信度：待确认 — `sumS1S2Product`在`IsDropMskCapable`中用于计算dropMask预期大小(line 273)；`actualSeqQlen/Kvlen`和`eaqualActSeqLen`未被BSH路径初始化。当前`IsDropMskCapable`对有dropMask的场景总是返回false，因此实际不会触发错误，但属于latent bug

问题代码：
```cpp
fBaseParams.t1 = fBaseParams.b * fBaseParams.s1;
return ge::GRAPH_SUCCESS;
```

BSH路径直接return，未初始化`t2`、`dv`、`sumS1S2Product`、`actualSeqQlen`、`actualSeqKvlen`、`eaqualActSeqLen`等字段。若后续扩展支持BSH+dropMask或BSH+d!=dv场景，这些未初始化字段会导致问题。

修复建议：补充BSH路径下的字段初始化：
```cpp
fBaseParams.t2 = fBaseParams.b * fBaseParams.s2;
fBaseParams.dv = fBaseParams.d; // 或从valueShape计算
fBaseParams.sumS1S2Product = fBaseParams.b * fBaseParams.s1 * fBaseParams.s2;
```

---

## 总结

本MR存在多个严重的正确性问题，BSH路径在当前状态下无法正常工作：最关键的是`set_s1()`/`set_s2()`从未在tiling中调用导致kernel侧所有BSH地址计算基于0值，其次是`getSeqLen`/`getTotalLen`无法区分Q和K序列、`sfmgOffset`/`softMaxOffset`缺少batch维度乘数，以及enum缺少分号导致编译失败。
建议优先修复8个严重问题（其中7个确定、1个待确认），然后清理调试输出和注释代码。
