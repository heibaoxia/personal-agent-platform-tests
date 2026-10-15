---
agentId: "path-escape-traversal"
contractVersion: "1"
purpose: "违规样例：索引路径用 .. 穿越出约定目录，预期触发路径逃逸错误。"
skills:
  - id: "escape-check"
    path: ".agent/../.agent/skills/escape-check.md"
---

# Personal Agent（违规样例：路径逃逸，.. 穿越）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的字段、类型、版本、slug 和标识唯一性都合法。

## 触发的违规

`skills` 索引项的 `path` 包含 `..` 路径段：`.agent/../.agent/skills/escape-check.md`。
即使归一化后指向 `.agent/skills/` 内的真实文件，路径本身也已经离开约定目录前缀。

- 预期错误类别：路径逃逸，稳定错误码 `SCAFFOLD_PATH_ESCAPE`（含 `.`/`..` 段即拒绝）。
- 该索引项被路径检查提前跳过，不再登记，因此磁盘上的 `escape-check.md` 无人登记，
  会一并报告 `SCAFFOLD_DISK_UNREGISTERED`（次要，同源于这条非法路径）。
- 目标文件在磁盘上存在，且 `id` 与文件名一致，不产生缺文件或标识不一致错误。

## 校验边界

校验只对路径做静态归一化与段检查，不访问工作空间之外的任何位置。
