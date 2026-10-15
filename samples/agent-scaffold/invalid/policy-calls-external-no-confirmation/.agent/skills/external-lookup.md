---
id: "external-lookup"
purpose: "声明一次受确认保护的外部检索，用于获取工作空间之外且可能过期的信息。"
triggers:
  - "用户询问工作空间之外、且可能随时间变化的事实"
excludes:
  - "不用于只需读取本工作空间信息的任务"
  - "不在未确认时自行发起调用"
inputs:
  - "需要检索的问题描述"
outputs:
  - "带来源标注的摘要"
sideEffects: "calls-external"
confirmation: "not-required"
implementation: "宿主按声明执行，本 change 只声明元数据，不提供实现。"
---

# Skill: external-lookup

## 用途

在需要外部信息时，声明一次可被审查的外部检索。

## 触发的违规

本文件声明会调用外部服务，却把 `confirmation` 写成 `not-required`，
与「副作用不是 `none` 时确认必须为 `required`」的契约要求冲突。

**边界**：本文件只声明元数据，不包含端点、凭据或启动命令。
