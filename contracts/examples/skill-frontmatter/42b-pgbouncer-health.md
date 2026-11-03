---
# source: 42b-example-papi-skill.md "## pgbouncer-health"
name: pgbouncer-health
description: >-
  Connection-pool health of a pgbouncer: waiting clients, busy server connections.
  Use when an application behind pgbouncer is slow or timing out on the database.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment]
    labels: { app.kubernetes.io/component: pgbouncer }
---
Waiting clients while every server connection is busy usually means stuck
transactions. Find which application holds them (for RunWhen: the task worker).
