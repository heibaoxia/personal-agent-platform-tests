---
agentId: "invalid-missing-field-skill"
contractVersion: "1"
purpose: "违规样例：Skill 声明缺少必填字段 sideEffects，预期触发对象缺字段错误。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
---

# Personal Agent（违规样例：Skill 缺字段）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法，`index` 与磁盘文件双向一致。

## 触发的违规

`.agent/skills/quiet-check.md` 的 frontmatter 缺少必填字段 `sideEffects`，
因此该 Skill 的字段集合不完整。

- 预期错误类别：对象缺字段，稳定错误码 `SCAFFOLD_FIELD_MISSING`（报告在 Skill 文件上）。
- 该 Skill 的 `id` 与文件名一致，不含枚举、路径、引用或策略类违规。

## 校验边界

校验只做静态字段检查，不执行 Skill、不访问网络、也不判断宿主运行时是否可用。
