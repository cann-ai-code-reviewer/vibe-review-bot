# Vibe Review

CANN 仓自动检视机器人。通过 Claude Code 管道模式配合自定义的 [vibe-review skill](https://github.com/tsukiyokai/vibe-review-skill)，在审查 PR diff 时同时按需读取上下文代码（不只看 diff 本身），并将检视意见发布为 GitCode PR 评论。

核心特性：

- **上下文感知**：审查时不仅看 diff，还会读取本地相关源码进行交叉验证
- **自动发布**：检视结果可直接发布为 GitCode PR 评论，支持 inline 逐行评论
- **持续轮询**：`review_loop.sh` 每 60 秒检查 team 成员的新 push，自动触发审查
- **统计追踪**：内置采纳率统计和检视意见追踪，支持历史数据导入

维护者：@tsukiyokai
Slack：[#vibereview](https://claude-rfj1883.slack.com/archives/C0AHLUT5E0M)

## 快速开始

前置条件：Python 3.10+，已安装 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI。

```bash
# 1. 克隆仓库
git clone https://github.com/tsukiyokai/vibe-review-bot.git
cd vibe-review-bot

# 2. 安装 vibe-review skill
npx skills add cann-ai-code-reviewer/vibe-review-skill

# 3. 设置 GitCode 个人访问令牌
export GITCODE_TOKEN=your_token

# 4. 审查一个 PR（默认仓库 hcomm，可通过 --repo 指定）
python3 ai_reviewer.py --repo hcomm --pr 1150
```

## 项目结构

```
ai_reviewer.py       # 核心：GitCode API、diff 拉取、Claude 调用、评论发布
review_loop.sh       # 轮询守护脚本
teams/               # 团队成员名单（按仓库命名，如 hcomm.txt）
log/                 # 检视产出，按仓库组织
doc/best_practice.md # 踩坑记录与部署经验
```

## 文档

- [用法详解](docs/usage.md) — 审查 PR、输出控制、本地文件/目录、持续轮询、统计追踪
- [配置说明](docs/configuration.md) — 配置项、teams 文件格式、项目结构详解
- [开发历程与功能清单](docs/development.md) — milestone 时间线 + 已完成/待办功能
- [Roadmap](docs/roadmap.md) — 效果优化、混合方法探索、成本分析、延伸阅读

## 参与贡献

所有变更走 PR，不直接 push `main`。

1. 创建分支：`git checkout -b your-feature`
2. 本地改好后测试：`python3 ai_reviewer.py --pr <any_pr> --dry-run`
3. 提 PR，写清楚改了什么、为什么改
4. 维护者 review 后 merge

适合上手的贡献：总结本组误报和高价值检视意见及 DTS 缺陷模式，反馈并闭环到 skill、修复你碰到的 bug。

沟通约定：通过 issue 异步交流。

## License

MIT
