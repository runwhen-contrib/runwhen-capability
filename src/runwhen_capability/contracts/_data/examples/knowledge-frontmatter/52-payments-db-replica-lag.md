---
# source: 52-rules-and-knowledge.md "## Knowledge (D61)"
name: payments-db-replica-lag
description: Replica lag above 30 s is normal during the nightly batch
resources:
  - kubernetes/clusters/prod-east/namespaces/payments/deployments/payments-db
tags: [postgres, lag]
---
Replica lag on payments-db rises above 30 s between 01:00 and 02:00 UTC while the batch runs. Don't page on it in that window.
