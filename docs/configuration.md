# 配置说明

## 项目结构详解

```
ai_reviewer.py            # 核心：GitCode API、diff 拉取、Claude 调用、评论发布
review_loop.sh            # 轮询守护脚本
teams/                    # 团队成员名单（按仓库命名，如 hcomm.txt）
doc/best_practice.md      # 踩坑记录与部署经验
log/                      # 检视产出，按仓库组织：
  └── cann/
      └── <repo>/
          ├── pr_1150/    #   ad4019.md, ad4019_diff.md (按 commit hash 保存)
          ├── by_file/    #   foo_cpp_review.md
          └── by_dir/     #   module_review.md
```

## 配置项

| 配置            | 说明                                                                              |
| --------------- | --------------------------------------------------------------------------------- |
| `GITCODE_TOKEN` | GitCode 个人访问令牌（环境变量或 `--token` 参数，不写入 config.yaml）             |
| `config.yaml`   | 所有可调参数（owner、repos_root、api_base、max_diff_chars 等），见文件注释        |
| `--repo`        | 目标仓库名，同时决定本地路径 `~/repo/cann/<repo>/` 和 GitCode API 目标 `cann/<repo>` |
| `--match`       | 只审查标题包含该关键字的 PR（全字匹配，大小写不敏感，`--pr` 模式下忽略）          |
| `teams/*.txt`   | 团队成员名单，按仓库命名（如 `hcomm.txt`），不纳入 git 托管，需自行创建。格式见下方说明 |

## teams 文件格式

每行一人，首行为标题行，空行和 `#` 开头的行会被忽略。

```
姓名      gitcode
张三      zhangsan
李四      lisi123
```

轮询脚本 `review_loop.sh` 和 `--team` 参数都依赖此文件来筛选团队成员的 PR。该文件已加入 `.gitignore`，克隆仓库后需自行创建。
