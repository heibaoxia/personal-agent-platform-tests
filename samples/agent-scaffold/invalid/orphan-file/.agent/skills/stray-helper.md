---
id: "stray-helper"
purpose: "一个本应被登记却漏登记的 Skill，用来触发孤立文件错误。"
triggers:
  - "用户要求核对工作空间结构"
excludes:
  - "不执行、不调用任何外部工具"
inputs:
  - "工作空间根目录路径"
outputs:
  - "一句结论"
sideEffects: "none"
confirmation: "not-required"
implementation: "宿主按声明执行，本 change 只声明元数据，不提供实现。"
---

# Skill: stray-helper

## 用途

本文件自身字段完整、内容非空，唯一的问题是它没有被 `AGENTS.md` 的 `skills` 索引登记。

## 触发的违规

约定目录 `.agent/skills/` 内的对象必须被登记；本文件故意缺席于索引，
构成孤立文件，预期错误码 `SCAFFOLD_DISK_UNREGISTERED`。

**边界**：静态校验只报告未登记对象。
