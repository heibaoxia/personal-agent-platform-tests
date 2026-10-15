---
agentId: "path-escape-absolute"
contractVersion: "1"
purpose: "违规样例：索引路径写成绝对路径，预期触发绝对路径与路径逃逸错误。"
skills:
  - id: "absolute-check"
    path: "C:/agent-workspace/skills/absolute-check.md"
---

# Personal Agent（违规样例：路径逃逸，绝对路径）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读。
本样例的字段、类型、版本、slug 和标识唯一性都合法。

## 触发的违规

`skills` 索引项的 `path` 是绝对路径 `C:/agent-workspace/skills/absolute-check.md`，
既不是相对路径，也必然落在工作空间之外。

- 预期错误类别：绝对路径，稳定错误码 `SCAFFOLD_PATH_ABSOLUTE`（报告在 `AGENTS.md`）。
- 该索引项被路径检查提前跳过，不会去探测工作空间外的文件，
  因此**不**产生缺文件错误（见 `expected-errors.yaml` 的「路径违规的终止规则」）。
- 同理，磁盘上的 `absolute-check.md` 无人登记，会一并报告
  `SCAFFOLD_DISK_UNREGISTERED`（次要，同源于这条非法路径）。

## 校验边界

校验只对路径字符串做静态判断，绝不读取工作空间之外的位置。
