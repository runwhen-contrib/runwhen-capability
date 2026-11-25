---
# expect: /runwhen/appliesTo 'types' is a required property
name: papi-health
description: Health of papi beyond its probes.
runwhen:
  appliesTo:
    labels: { app.kubernetes.io/component: papi }
---
Body.
