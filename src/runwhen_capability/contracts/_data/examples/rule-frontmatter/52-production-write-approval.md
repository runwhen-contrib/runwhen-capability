---
# source: 52-rules-and-knowledge.md "## Rules (D59, D60)"
name: production-write-approval   # fixture: name and description written for the fixture; the body is the quoted rule
description: Write tasks against production namespaces need the change named in the approval reason
---
Never run a write task against a namespace labelled `environment=production` without naming the change in the approval reason.
