---
id: "review"
purpose: "把只读复核拆成两个顺序步骤，并说明每步的输入与产出。"
steps:
  - "读取 AGENTS.md 的 frontmatter 索引。"
  - "按索引逐项核对对象文件是否存在且字段完整。"
---

# Workflow: review

## 用途

把一次复核拆成两个可静态阅读的顺序步骤。

## 说明

本文件自身合法：字段完整、步骤不少于两步，且没有引用任何 Skill，
因此不产生悬空引用。违规来自它与 Skill 共用 id `review`。

**边界**：本 Workflow 只被静态声明，宿主是否会执行它不属于本 change 的验收范围。
