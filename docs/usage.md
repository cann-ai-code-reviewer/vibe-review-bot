# 用法详解

## 审查 PR

```bash
python3 ai_reviewer.py                                     # 最近 3 个 open PR
python3 ai_reviewer.py --pr 1150 1144                      # 指定 PR
python3 ai_reviewer.py --author lilin_137 -n 0             # 某用户的全部 open PR
python3 ai_reviewer.py --state merged --count 3            # 已合并的 PR
python3 ai_reviewer.py --repo ops-transformer --pr 2071    # 其他 CANN 仓库
python3 ai_reviewer.py --match PLZ                         # 只审查标题含 PLZ 的 PR
```

## 输出控制

审查结果默认输出到终端，可通过以下标志控制：

```bash
python3 ai_reviewer.py --pr 1150 --save               # 保存到 log/
python3 ai_reviewer.py --pr 1150 --comment            # 发布评论到 GitCode PR
python3 ai_reviewer.py --pr 1150 --comment --force    # 强制重审（忽略"已审查过"）
```

## 审查本地文件

不需要 GitCode token：

```bash
python3 ai_reviewer.py --file src/foo.cpp src/bar.h --save
python3 ai_reviewer.py --dir src/framework/zero_copy/ --save
```

## 持续轮询

```bash
export GITCODE_TOKEN=your_token
bash review_loop.sh hcomm teams/hccl.txt          # 轮询审查全部 PR
bash review_loop.sh hcomm teams/hccl.txt PLZ      # 只审查标题含 PLZ 的 PR
```

每 60 秒检查 team 成员是否有新 push，有则自动触发审查并发布评论。

## 统计与追踪

```bash
python3 ai_reviewer.py --stats --days 90    # 采纳率统计
python3 ai_reviewer.py --track --pr 1150    # 追踪单个 PR 的检视意见
python3 ai_reviewer.py --import-logs        # 导入历史审查日志到追踪 DB
```

选项可组合：`--author` 按用户筛选，`--count`/`-n` 限制数量，`--state` 筛选 PR 状态，`--match` 按标题关键字筛选，`--dry-run` 只拉取不审查。
