---
id: "quiet-check"
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

# Skill: quiet-check

## 用途

对工作空间中已经写下的声明做一次只读的一致性核对。

## 说明

本文件自身完全合法：id 与文件名一致，字段、类型、枚举和策略声明都合规。
违规由入口索引重复登记同一个文件触发。

**边界**：静态校验不执行本 Skill。
