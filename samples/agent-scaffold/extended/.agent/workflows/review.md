---
id: "review"
purpose: "对一次待落地的变更做静态复核，并把声明层面和运行时层面的问题分开报告。"
inputs:
  - "待复核的变更范围，例如工作空间路径或变更描述"
outputs:
  - "按严重程度排序的发现列表"
  - "未覆盖项和需要后续 change 处理的问题"
steps:
  - "确认复核范围，并读出工作空间根目录 AGENTS.md 的 frontmatter 索引。"
  - "用 quiet-check 核对索引与对象文件是否双向一致。"
  - "逐项核对上下文、行为、能力、编排和外部依赖的边界是否被混淆。"
  - "对需要外部信息的部分，按 external-lookup 的声明判断是否具备确认与降级前置条件。"
  - "输出发现列表，并显式标注哪些属于静态声明、哪些需要运行时验证。"
usesSkills:
  - "quiet-check"
  - "external-lookup"
---

# Workflow: review

## 用途

把一次复核拆成可重复的步骤，并保证结论里不把静态声明当成运行时能力。

## 何时使用

- 变更即将落地，需要一次结构层面的复核。
- 需要确认新增对象已经登记且引用可以解析。

## 步骤

1. 确认复核范围，读取 `AGENTS.md` 的 frontmatter 索引。
2. 用 `quiet-check` 核对索引与磁盘对象是否双向一致。
3. 核对 Context、Behavior、Skill、Workflow、Integration 的边界是否被混淆。
4. 对涉及外部信息的步骤，检查确认声明和降级策略是否齐备。
5. 输出发现列表，区分静态问题和需要运行时验证的问题。

## 引用

`usesSkills` 引用两个已声明的 Skill：`quiet-check` 和 `external-lookup`。

## 边界

本文件只声明步骤和引用，不定义执行引擎、调度、重试或状态保存。
步骤是否被真正执行由宿主运行时决定，本 change 不实现 Workflow 执行。
