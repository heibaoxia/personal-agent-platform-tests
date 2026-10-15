---
id: "lookup"
purpose: "Exercise purpose line validation."
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
  - id: "scope"
    purpose: "first\nsecond"
    from: "user"
    ifMissing: "ask"
---

# Invalid multiline purpose
