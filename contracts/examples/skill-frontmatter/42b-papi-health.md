---
# source: 42b-example-papi-skill.md "## papi-health"
name: papi-health
description: >-
  Health of a self-hosted RunWhen platform API (papi) beyond its probes: API latency
  and task-worker activity. Use when RunWhen pages hang or time out, background jobs
  stop, or papi's probes pass but requests fail.
runwhen:
  role: health
  appliesTo:
    types: [kubernetes/deployment]
    labels: { app.kubernetes.io/component: papi }
  tags: [runwhen, papi, availability]
---
## What each task tells you
- **api-probe**: whether papi answers and how fast. A pass does *not* rule out a wedge,
  because `/healthz` never touches the database.
- **worker-activity**: whether the task worker has done any work recently.

## Check the database too
Slow API or an idle worker: follow papi's dependencies to its pgbouncer Deployment and
run `runwhen-platform-ops:pgbouncer-health/pool-saturation` there.

## Reading them together
Slow API + a `saturated` pool + idle worker = a hung worker holding row locks.
Run `runwhen-platform-ops:papi-recovery/restart-worker`. Don't restart papi itself.
