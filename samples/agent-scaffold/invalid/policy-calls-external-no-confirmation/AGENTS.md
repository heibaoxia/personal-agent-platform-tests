---
agentId: "policy-calls-external-no-confirmation"
contractVersion: "1"
purpose: "违规样例：Skill 调用外部服务但声明无需确认，预期触发副作用与确认策略冲突。"
skills:
  - id: "external-lookup"
    path: ".agent/skills/external-lookup.md"
---

# Personal Agent（违规样例：调用外部服务但无需确认）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。

## 触发的违规

`external-lookup` 声明 `sideEffects: "calls-external"`，却同时声明
`confirmation: "not-required"`。副作用不是 `none` 时确认必须为 `required`。

- 预期错误类别：副作用与确认策略冲突，稳定错误码 `SCAFFOLD_POLICY_CONFIRMATION`。
- 本样例故意不写 `integrations` 索引：若声明 Integration，就必须同时提供那个文件，
  引入与目标类别无关的缺文件风险，因此这里只触发确认策略冲突。
- `calls-external` 缺少 Integrations 索引时还会触发 `SCAFFOLD_POLICY_EXTERNAL_INTEGRATION`
  （次要，同源于同一处策略声明，见覆盖表），专门的最小样例见
  `policy-calls-external-no-integration`。

## 校验边界

校验只检查声明一致性，绝不连接外部服务、不读取凭据。
