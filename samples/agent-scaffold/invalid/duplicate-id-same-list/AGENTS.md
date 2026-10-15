---
agentId: "duplicate-id-same-list"
contractVersion: "1"
purpose: "违规样例：同一 skills 索引里两条索引项指向同一个文件，预期触发重复标识错误。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
  - id: "quiet-check-alias"
    path: ".agent/skills/quiet-check.md"
---

# Personal Agent（违规样例：重复 id，同列表）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。

## 触发的违规

`skills` 索引里两条索引项分别使用 `quiet-check` 与 `quiet-check-alias` 两个 id，
但两条都指向同一个对象文件 `.agent/skills/quiet-check.md`：
磁盘上的一个对象被登记了两次。

- 预期错误类别：重复标识，稳定错误码 `SCAFFOLD_DUPLICATE_ID`
  （校验器在「同一路径被声明两次」这一处报告，提示语形如 `path ... is declared twice`）。
- 两个 id 本身都是合法 slug，且都只出现一次，不触发 slug 类错误。
- 被指向的文件存在且 `id` 与文件名一致，不触发文件名标识错误。
- 该文件有索引项登记，不触发孤立文件错误。

## 校验边界

校验只比对索引结构，不自动删除或合并重复项。
