---
agentId: "policy-calls-external-no-integration"
contractVersion: "1"
purpose: "违规样例：Skill 调用外部服务但入口没有 Integration 索引，预期触发外部能力缺少集成声明错误。"
skills:
  - id: "external-lookup"
    path: ".agent/skills/external-lookup.md"
---

# Personal Agent（违规样例：外部 Skill 缺 Integration）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法。

## 触发的违规

`external-lookup` 声明 `sideEffects: "calls-external"` 且 `confirmation: "required"`
（确认策略本身合规），但入口的 frontmatter 完全没有 `integrations` 索引项：
调用外部服务的 Skill 没有任何外部依赖声明。

- 预期错误类别：外部能力缺少集成声明，稳定错误码 `SCAFFOLD_POLICY_EXTERNAL_INTEGRATION`
  （报告在 Skill 文件的 `sideEffects` 行）。
- 本样例不声明 integration 文件，因此不产生缺文件或孤立文件错误。
- 确认策略正确，不触发 `SCAFFOLD_POLICY_CONFIRMATION`。

## 校验边界

校验只读声明，不连接外部服务，也不判断某次真实调用是否会失败。
