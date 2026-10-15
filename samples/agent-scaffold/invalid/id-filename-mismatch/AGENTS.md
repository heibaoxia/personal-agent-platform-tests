---
agentId: "id-filename-mismatch"
contractVersion: "1"
purpose: "违规样例：Skill 的 id 与文件名不一致，预期触发标识与文件名不一致错误。"
skills:
  - id: "mismatch-check"
    path: ".agent/skills/other-name.md"
---

# Personal Agent（违规样例：id 与文件名不一致）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。

## 触发的违规

索引项声明的 id 是 `mismatch-check`，路径指向 `.agent/skills/other-name.md`；
该文件内的 `id` 也是 `mismatch-check`，但文件名主干是 `other-name`。

- 预期错误类别：标识与文件名不一致，稳定错误码 `SCAFFOLD_FILE_ID_MISMATCH`
  （报告在 Skill 文件上；索引 id 与文件内声明 id 一致，因此不报 `SCAFFOLD_INDEX_ID_MISMATCH`）。
- 索引 id 与文件内 id 一致，路径相对且位于约定目录内，不触发路径类错误。
- 目标文件存在，不触发缺文件错误；该文件有索引项登记，不触发孤立文件错误。

## 校验边界

校验只比较声明与文件名，不重命名文件，也不修改 frontmatter。
