---
id: "review"
purpose: "把一次复核拆成两个顺序步骤，并引用一个尚未声明的 Skill。"
steps:
  - "读取 AGENTS.md 的 frontmatter 索引。"
  - "调用 missing-helper 复核索引与磁盘对象是否一致。"
usesSkills:
  - "missing-helper"
---

# Workflow: review

## 用途

把一次复核拆成两个可静态阅读的顺序步骤。

## 触发的违规

`usesSkills` 引用了 `missing-helper`，该 Skill 没有被入口索引登记，
磁盘上也没有对应文件，属于悬空引用，预期错误码 `SCAFFOLD_REF_DANGLING`。

**边界**：本 Workflow 只被静态声明，宿主是否会执行、如何解析引用不属于本 change 的验收范围。
