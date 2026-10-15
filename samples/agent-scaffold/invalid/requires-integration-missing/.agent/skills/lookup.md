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
  - id: "search"
    purpose: "Need an undeclared Integration."
    from: "integration"
    integration: "missing-search"
    ifMissing: "ask"
---

# Invalid requires fixture
