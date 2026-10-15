---
id: "review"
purpose: "对一个已声明的工作空间结构做只读复核，并给出简短结论。"
triggers:
  - "用户要求复核工作空间结构"
excludes:
  - "不执行、不调用任何外部工具"
inputs:
  - "工作空间根目录路径"
outputs:
  - "一句复核结论"
sideEffects: "none"
confirmation: "not-required"
implementation: "宿主按声明执行，本 change 只声明元数据，不提供实现。"
---

# Skill: review

## 用途

对工作空间声明做一次只读复核。

## 说明

本文件自身合法，违规来自 `review` 这个 id 同时被 Workflow 复用。

**边界**：静态校验不执行本 Skill。
