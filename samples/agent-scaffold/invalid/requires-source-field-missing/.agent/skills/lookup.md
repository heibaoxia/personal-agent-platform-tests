---
id: "lookup"
purpose: "Fixture Skill for requires validation."
triggers:
  - "test"
excludes:
  - "test"
inputs:
  - "question"
outputs:
  - "answer"
sideEffects: "none"
confirmation: "not-required"
implementation: "Fixture only."
requires:
  - id: "context-missing"
    purpose: "Context category is required."
    from: "context"
    ifMissing: "ask"
  - id: "integration-missing"
    purpose: "Integration id is required."
    from: "integration"
    ifMissing: "ask"
  - id: "user-extra"
    purpose: "User source cannot carry Context fields."
    from: "user"
    contextCategory: "active-task"
    ifMissing: "ask"
---

# Invalid requires fixture
