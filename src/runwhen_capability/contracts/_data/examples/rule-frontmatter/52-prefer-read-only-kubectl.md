---
# source: 52-rules-and-knowledge.md "## Rules (D59, D60)"
name: prefer-read-only-kubectl   # fixture: name and description written for the fixture; the body is the quoted rule
description: Use the read-only kubectl verbs rather than exec
---
Prefer `kubectl get`, `describe` and `logs` over `exec`.
