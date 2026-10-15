---
id: "notes-writer"
purpose: "把一条已经确认的结论写入工作空间内的记录文件。"
triggers:
  - "用户要求把确认过的结论记入工作空间"
excludes:
  - "不在用户未确认时写入任何内容"
  - "不调用工作空间之外的任何服务"
inputs:
  - "要记录的结论"
outputs:
  - "工作空间内的记录文件变更说明"
sideEffects: "writes-workspace"
confirmation: "not-required"
implementation: "宿主按声明执行，本 change 只声明元数据，不提供实现。"
---

# Skill: notes-writer

## 用途

把一条已经确认的结论写进工作空间内的记录文件。

## 触发的违规

本文件声明会写入工作空间，却把 `confirmation` 写成 `not-required`，
与「副作用不是 `none` 时确认必须为 `required`」的契约要求冲突。

**边界**：静态校验不执行写入，也不判断宿主是否真的弹出确认。
