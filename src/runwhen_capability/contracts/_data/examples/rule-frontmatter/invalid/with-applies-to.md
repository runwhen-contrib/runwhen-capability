---
# expect: "" 'appliesTo' was unexpected
name: prefer-read-only-kubectl
description: Use the read-only kubectl verbs rather than exec
appliesTo: { types: [kubernetes/pod] }
---
Prefer `kubectl get`, `describe` and `logs` over `exec`.
