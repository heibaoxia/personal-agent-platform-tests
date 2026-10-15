---
agentId: "personal-agent-minimal"
contractVersion: "1"
purpose: "最小 Agent 工作空间样例，只声明一个无副作用 Skill，保持契约可校验的最小结构。"
skills:
  - id: "quiet-check"
    path: ".agent/skills/quiet-check.md"
---

# Personal Agent（minimal）

## 本文件的作用

本文件是工作空间的唯一必需入口。上方 frontmatter 是索引的机器真相，正文只用于阅读：
正文说明与 frontmatter 冲突时，以 frontmatter 为准。

## 结构

```text
minimal/
├── AGENTS.md
└── .agent/skills/quiet-check.md
```

本样例是最小形态。没有 Context、Behavior、Workflow 或 Integration —— 这些对象按需创建，
不存在时就不出现，不以空目录、空文件或占位配置替代。

## 扩展边界

需要新增能力时：

- 增加对象文件，并在 frontmatter 的 `skills`、`workflows`、`integrations` 索引中登记。
- `id` 使用小写 slug，且与文件名一致；`path` 是位于约定目录内的相对路径。
- 只声明当前真正使用的能力，不为未来需求预留空结构。
- Skill 声明 `sideEffects` 时，非 `none` 必须同时声明 `confirmation: "required"`；
  调用外部服务时工作空间必须至少声明一个 Integration。

## 校验边界

本工作空间只被静态校验：字段、枚举、标识、路径和引用可以检查。
校验通过不代表宿主运行时可用，也不代表 Skill 会被路由或执行。
