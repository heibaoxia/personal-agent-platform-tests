---
agentId: "duplicate-id-across-types"
contractVersion: "1"
purpose: "违规样例：Skill 与 Workflow 使用同一个 id review，预期触发跨对象类型重复标识错误。"
skills:
  - id: "review"
    path: ".agent/skills/review.md"
workflows:
  - id: "review"
    path: ".agent/workflows/review.md"
---

# Personal Agent（违规样例：重复 id，跨对象类型）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。

## 触发的违规

`skills` 与 `workflows` 两个索引各自合法，但同一个 id `review` 同时被两类对象声明：
工作空间内的标识不再唯一，引用无法确定指向哪个对象。

- 预期错误类别：重复标识，稳定错误码 `SCAFFOLD_DUPLICATE_ID`。
- 两个 id 都是合法 slug，且各自在自己列表内只出现一次。
- 两个目标文件都存在，且各自 `id` 与文件名一致。
- 当前校验器使用统一的对象标识空间，会报告 `SCAFFOLD_DUPLICATE_ID`；这不是运行时路由或自动重命名。

## 校验边界

校验只判断标识唯一性，不替使用者重命名对象。
