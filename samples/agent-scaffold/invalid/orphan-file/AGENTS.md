---
agentId: "orphan-file"
contractVersion: "1"
purpose: "违规样例：约定目录内存在未登记的 Skill 文件，预期触发孤立文件错误。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
---

# Personal Agent（违规样例：孤立文件）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法。

## 触发的违规

约定目录 `.agent/skills/` 内除了已登记的 `quiet-check.md`，还放着 `stray-helper.md`；
该文件没有出现在 `skills` 索引中，属于未登记对象（孤立文件）。

- 预期错误类别：孤立文件，稳定错误码 `SCAFFOLD_DISK_UNREGISTERED`。
- `quiet-check` 已登记且存在，`stray-helper` 自身字段也完整，不含字段或枚举违规。
- 本样例不故意构造指向 `stray-helper` 的引用，避免引入悬空引用类错误。

## 校验边界

校验只报告未登记对象，不替使用者登记或删除文件。
