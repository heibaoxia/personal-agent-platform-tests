---
agentId: "dangling-reference"
contractVersion: "1"
purpose: "违规样例：Workflow 引用未声明的 Skill，预期触发悬空引用错误。"
workflows:
  - id: "review"
    path: ".agent/workflows/review.md"
---

# Personal Agent（违规样例：悬空引用）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的入口字段、类型、版本、slug 和索引路径都合法。

## 触发的违规

`.agent/workflows/review.md` 用 `usesSkills` 引用 `missing-helper`，
但该 id 既不在入口索引中，也不存在对应文件，引用无法解析。

- 预期错误类别：悬空引用，稳定错误码 `SCAFFOLD_REF_DANGLING`（报告在 `review.md`）。
- 被引用的 `missing-helper` 从未登记，因此也不产生孤立文件错误。
- 本样例只声明 Workflow，不额外放置未被引用的 Skill 文件。

## 校验边界

校验只解析声明之间的引用，不推断引用意图，也不补建缺失对象。
