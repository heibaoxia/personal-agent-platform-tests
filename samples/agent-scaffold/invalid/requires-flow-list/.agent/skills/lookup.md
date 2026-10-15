---
id: "lookup"
purpose: "Exercise requires parser boundaries."
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
requires: [{id: "question", purpose: "Need the question.", from: "user", ifMissing: "ask"}]
---

# Invalid flow list
