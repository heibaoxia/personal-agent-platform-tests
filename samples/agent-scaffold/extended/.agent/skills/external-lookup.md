---
id: "external-lookup"
workflow: "review"
purpose: "在需要工作空间之外的事实或时效性信息时，声明一次受确认保护的外部检索。"
triggers:
  - "用户询问工作空间之外、且可能随时间变化的事实"
  - "用户明确要求引用外部来源"
excludes:
  - "不用于只需读取本工作空间信息的任务"
  - "不替代用户确认，也不在未确认时自行发起调用"
  - "不适用于需要凭据或私有端点的场景"
inputs:
  - "需要检索的问题描述"
  - "可接受的来源约束，例如语言或时间范围"
requires:
  - id: "problem-statement"
    purpose: "明确要核实的问题，避免把外部检索用于可由工作空间回答的任务。"
    from: "user"
    ifMissing: "ask"
  - id: "active-task"
    purpose: "确认当前任务范围，限制外部检索只服务于已确认的工作。"
    from: "context"
    contextCategory: "active-task"
    ifMissing: "refuse"
  - id: "search-channel"
    purpose: "确认宿主提供可用的外部检索通道。"
    from: "integration"
    integration: "search"
    ifMissing: "degrade"
    note: "只报告工作空间内可核实的事实，并标注未覆盖部分。"
outputs:
  - "带来源标注的摘要"
  - "无法核实的结论和剩余不确定项"
sideEffects: "calls-external"
confirmation: "required"
implementation: "由宿主适配器对接 integration: search 实现；本 change 只声明元数据，不提供实现。"
---

# Skill: external-lookup

## 用途

在明确需要外部信息时，声明一次可被审查的外部检索：先说明要查什么，再在用户确认后调用。

## 何时使用

- 问题涉及工作空间之外的事实，且答案可能随时间变化。
- 用户希望结论带有可追溯的来源。

## 何时不使用

- 只读本工作空间就能回答的问题。
- 用户没有确认调用外部服务。
- 场景需要凭据、私有端点或无法公开的来源。

## 输入与输出

- 输入：问题描述和来源约束。
- 输出：带来源标注的摘要，并显式列出无法核实的部分。

## 副作用与确认

`sideEffects` 为 `calls-external`，因此 `confirmation` 为 `required`，且工作空间必须声明
至少一个 Integration —— 本样例声明的是 `search`。`workflow` 指向已声明的 `review`。

`requires` 只声明开始执行前需要掌握的事实：问题描述来自用户，任务范围来自 Context，
检索通道来自 Integration。缺失时分别追问、停止或按 `note` 描述的结果降级；校验器只检查
这些字段和引用，不执行追问、读取 Context 或连接外部服务。

## 边界

本文件只声明元数据。它不包含端点、凭据或启动命令，也不代表外部服务可用；
真实调用与降级行为由后续宿主适配器 change 实现和验收。
