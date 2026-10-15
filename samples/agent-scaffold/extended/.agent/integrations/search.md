---
id: "search"
kind: "mcp"
purpose: "为 external-lookup 提供外部检索能力的外部依赖声明。"
requires:
  - "宿主提供名为 search 的 MCP 服务"
  - "用户在执行前明确确认"
fallback: "degrade"
fallbackNote: "外部检索不可用时改为只读本工作空间信息，并显式标注结论未经过外部核实。"
enabled: false
---

# Integration: search

## 用途

声明 `external-lookup` 所需要的外部检索依赖，以及它不可用时的处理方式。

## 静态声明

- `kind` 为 `mcp`，表示依赖由宿主提供的 MCP 服务。
- `requires` 列出前置条件：宿主提供该服务，并且用户在执行前确认。
- `enabled` 为 `false`：本样例不启用任何真实连接，只演示声明形态。
- 本文件不包含端点、凭据、命令或任何可执行连接配置。

## 降级策略

`fallback` 为 `degrade`，因此 `fallbackNote` 必须非空：外部检索不可用时，
改为只读本工作空间信息回答，并在结论中标注未经外部核实。
若某场景必须依赖外部来源才能给出结论，应改为 `block` 并停止推进，而不是猜测。

## 边界

本文件只作为静态声明被读取。校验器不会连接服务、读取凭据或启动进程；
真实工具是否可用、降级是否生效，由后续宿主适配器 change 实现和验收。
