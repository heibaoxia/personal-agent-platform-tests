---
agentId: "invalid-enum"
contractVersion: "1"
purpose: "违规样例：Skill 的 sideEffects 使用非法枚举值 network-call，预期触发枚举校验错误。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
---

# Personal Agent（违规样例：非法枚举）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法，`index` 与磁盘文件双向一致。

## 触发的违规

`.agent/skills/quiet-check.md` 声明 `sideEffects: "network-call"`，不在
`none`、`writes-workspace`、`calls-external` 三个允许值之内。

- 预期错误类别：非法枚举，稳定错误码 `SCAFFOLD_ENUM_INVALID`（报告在 `sideEffects` 行）。
- 该值不是 `none`，因此同时声明了 `confirmation: "required"`，不触发策略错误。
- 该值也不是 `calls-external`，因此不要求声明 Integration。

## 校验边界

校验只比对枚举字面量，不解释 `network-call` 的真实含义。
