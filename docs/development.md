# 开发历程与功能清单

## Milestone

> 2026 年 2-3 月，从手动 review 到全自动检视机器人。

**2/12 — 起步**：创建 vibe-review skill，基于 CANN C++ 编码规范。手动 curl 下载 PR diff，手动调用 skill 审查。

**2/13 — 探索输出形式**：确定 markdown 为标准输出格式。

**2/16 — 脚本诞生**：编写 review_prs.py（ai_reviewer.py 前身），通过 GitCode API 自动拉取 open PR diff 并调用 Claude Code 审查。支持指定 PR、按作者筛选。

**2/17 — 打通评论流程**：实现 `--comment` 将审查结果发布为 GitCode PR 评论。旧评论自动清理。添加 `--state` 支持已合并 PR、`--save` 控制本地保存。审查结果最短长度校验防止空报告。

**2/18 — 结构化与扩展**：建立 `log/by_pr`、`log/by_file` 目录结构。添加 `--file` 本地文件审查、`--author` 批量筛选。撰写 best_practice.md 踩坑博客。

**2/21 — 成本与并行**：添加 token 消耗和耗时统计、变更文件 LOC 显示(+/-)。实现多 PR 并行审查。上线 inline 模式（逐行评论到 GitCode 代码行）。

**2/22 — inline 攻坚**：多轮修复 inline 评论定位偏移问题。添加审查进度实时显示、`--clean` 清除 AI 评论、`--dir` 目录级审查。分析 200K context window 对审查质量的影响。

**2/24 — 团队化**：支持 team.txt 批量审查团队成员 PR、自动跳过 `[WIP]` 标记的 PR、短任务优先调度。基于 Claude 官方定价优化成本监控。审查报告代码块添加语法高亮。

**2/25 — 跨仓库与持续轮询**：脚本从 hcomm-dev/ 迁移到 jbs/ 独立目录。添加 `--repo` 参数支持跨仓库审查。实现基于 HEAD SHA 的重复检视防护（`--force` 强制重审）。编写 review_loop.sh 轮询守护脚本。

**2/26 — 生产加固**：review_loop.sh 完善（失败重试、变更检测优化）。评论发布后输出 GitCode 链接。创建 canndev skill 覆盖 PR 全生命周期。

**2/27 — 追踪统计**：实现 `--stats` 采纳率统计、`--track` 检视意见追踪、`--import-logs` 历史数据导入。行号统一为范围格式(199-201)。log 目录重构为 `log/cann/<repo>/` 层级。扩展支持 ops-transformer 仓库。轮询脚本失败恢复修复。

**2/28 — 开源与重构**：vibe-review skill 重构（渐进式加载、分层规范文件）。项目托管到 GitHub，编写 README。

**3/1 — 改名**：项目从 ai_code_review 重命名为 vibe-review，skill 从 codereview 重命名为 vibe-review。skill 内容纳入仓库版本管理（替换符号链接），添加 setup.sh 一键安装。

**3/3 — npm 发包**：vibe-review skill 提取为独立项目 [vibe-review-skill](https://github.com/tsukiyokai/vibe-review-skill)，发布到 npm（[@tsukiyokai/vibe-review](https://www.npmjs.com/package/@tsukiyokai/vibe-review)）。用户通过 `npx @tsukiyokai/vibe-review --global` 一键安装。vibe-review-bot 仓库不再包含 skill 源码，改为依赖 npm 包。

## Todos

- [x] 创建 vibe-review skill，基于 CANN C++ 编码规范
- [x] 通过 GitCode API 自动拉取 PR diff
- [x] 调用 Claude Code vibe-review skill 进行审查
- [x] 审查指定 PR (`--pr`)
- [x] 按作者筛选 PR (`--author`)
- [x] 审查结果发布为 GitCode PR 评论 (`--comment`)
- [x] 发布前自动清理旧的 AI 评论
- [x] 审查结果保存到本地 markdown (`--save`)
- [x] 审查结果最短长度校验，防止空报告
- [x] 支持已合并 PR 审查 (`--state merged`)
- [x] 审查本地文件 (`--file`)
- [x] 审查本地目录 (`--dir`)
- [x] log 目录结构：by_pr / by_file / by_dir
- [x] token 消耗和耗时统计（成本监控）
- [x] 变更文件 LOC 显示(+/-)
- [x] 多 PR 并行审查
- [x] inline 模式：逐行评论到 GitCode 代码行
- [x] 审查进度实时显示
- [x] 清除指定 PR 的 AI 评论 (`--clean`)
- [x] team.txt 批量审查团队成员 PR
- [x] 自动跳过 `[WIP]` 标记的 PR
- [x] 短任务优先调度
- [x] 基于 Claude 官方定价的成本计算
- [x] 审查报告代码块语法高亮
- [x] 跨仓库审查 (`--repo`)
- [x] 基于 HEAD SHA 防止重复检视
- [x] 强制重新审查 (`--force`)
- [x] review_loop.sh 轮询守护脚本
- [x] 评论发布后输出 GitCode 链接
- [x] 轮询脚本失败自动恢复
- [x] 采纳率统计 (`--stats`)
- [x] 检视意见追踪 (`--track`)
- [x] 历史审查数据导入 (`--import-logs`)
- [x] 行号范围格式统一(199-201)
- [x] log 目录按项目/仓库分层(`log/cann/<repo>/`)
- [x] 支持多个 CANN 仓库(hcomm、ops-transformer)
- [x] vibe-review skill 重构（渐进式加载、分层规范）
- [x] GitHub 托管与 README
- [x] 项目重命名为 vibe-review，skill 重命名为 vibe-review
- [x] skill 内容纳入仓库版本管理，添加 setup.sh 一键安装
- [x] vibe-review skill 提取为独立 [npm 包](https://www.npmjs.com/package/@tsukiyokai/vibe-review)（[GitHub](https://github.com/tsukiyokai/vibe-review-skill)）
- [x] 标题关键字过滤 (`--match`)，只审查标题含指定关键字的 PR
- [x] 从 hcomm 仓库 git 历史挖掘 HCCL 高价值缺陷模式：分析全部 428 次提交，识别 84 次缺陷提交，逐条分析根因和修复模式，产出 48 条审查规则覆盖 12 个缺陷类别（算法正确性、并发、内存、整数溢出、错误处理、资源生命周期等）+ 6 条跨类别系统性风险规则。规则已写入 skill 的 references/standards-project-hccl.md — 260302
- [x] 用上述方法完成 ops-transformer 代码仓分析，输出 references/standards-project-ops-transformer.md — 260303
- [ ] 支持 Gitee V5 API
- [ ] webhook 打通（跑个 HTTP server 来接收 GitCode 的 webhook 请求，部署复杂度 UP）
- [ ] cc 管道模式和交互模式的效果差异分析
- [ ] 与 CMC 合作形成一套检视意见反馈 skill 的方法论
- [ ] 采纳率算法优化（存储上使用了 Python 标准库的 sqlite3 模块，主要用于 PR 审查的跟踪数据库；算法上因为 diff 追踪算法还没完全实现出来所以数据不算数）
- [ ] 切内部模型
