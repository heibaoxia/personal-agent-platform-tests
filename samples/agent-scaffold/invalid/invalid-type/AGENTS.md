---
agentId: "invalid-type"
contractVersion: "1"
purpose: "违规样例：入口把 skills 索引写成标量而不是索引项列表，预期触发字段类型错误。"
skills: ".agent/skills/quiet-check.md"
---

# Personal Agent（违规样例：非法类型）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的 `agentId`、`contractVersion` 和 `purpose` 都合法。

## 触发的违规

`skills` MUST 是索引项列表，每项包含 `id` 与相对 `path`；
本样例把它写成单个标量字符串 `.agent/skills/quiet-check.md`。

- 预期错误类别：字段类型非法，稳定错误码 `SCAFFOLD_FIELD_TYPE`（报告在 `AGENTS.md`）。
- 标量没有 `id`，因此该 Skill 无法被索引登记，磁盘文件成为未登记对象，
  会一并报告 `SCAFFOLD_DISK_UNREGISTERED`（次要，同源于同一处类型错误）。
- 该 Skill 文件自身字段完整，不含枚举、标识或策略类违规。

## 校验边界

校验只判断结构类型，不猜测写作者的意图，也不自动改写索引。
