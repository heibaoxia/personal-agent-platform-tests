---
agentId: "policy-writes-workspace-no-confirmation"
contractVersion: "1"
purpose: "违规样例：Skill 会写入工作空间但声明无需确认，预期触发副作用与确认策略冲突。"
skills:
  - id: "notes-writer"
    path: ".agent/skills/notes-writer.md"
---

# Personal Agent（违规样例：写工作空间但无需确认）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法。

## 触发的违规

`notes-writer` 声明 `sideEffects: "writes-workspace"`，却同时声明
`confirmation: "not-required"`。契约要求副作用不是 `none` 时确认必须为 `required`。

- 预期错误类别：副作用与确认策略冲突，稳定错误码 `SCAFFOLD_POLICY_CONFIRMATION`。
- 该 Skill 的枚举值都在允许集合内，不触发枚举类错误。
- `writes-workspace` 不是 `calls-external`，因此不要求声明 Integration。

## 校验边界

校验只检查声明一致性，不代表运行时一定执行确认。
