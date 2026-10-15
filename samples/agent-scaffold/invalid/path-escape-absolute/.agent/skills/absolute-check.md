---
id: "absolute-check"
purpose: "在只读范围内核对工作空间声明的结构是否自洽，并返回一条简短结论。"
triggers:
  - "用户要求确认工作空间结构是否一致"
excludes:
  - "不执行、不调用任何外部工具"
inputs:
  - "工作空间根目录路径"
outputs:
  - "一句结论，说明结构声明是否自洽"
sideEffects: "none"
confirmation: "not-required"
implementation: "宿主按声明执行，本 change 只声明元数据，不提供实现。"
---

# Skill: absolute-check

## 用途

对工作空间中已经写下的声明做一次只读的一致性核对。

## 说明

本文件位于约定目录 `.agent/skills/` 内，但入口索引**没有**用它的真实相对路径登记它 ——
那条索引项写的是工作空间之外的绝对路径。因此本文件自身合规，违规在入口索引。

**边界**：静态校验不会读取工作空间之外的任何文件。
