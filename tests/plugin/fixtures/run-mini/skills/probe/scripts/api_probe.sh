#!/usr/bin/env bash
# the wiki 42b script, run with set -euo pipefail; `date +%s%3N` is GNU-only, so SECONDS stands in
ns="$RW_RESOURCE_NAMESPACE"; svc="$RW_RESOURCE_NAME"   # chart convention: the Service is named like the Deployment
for path in healthz livez; do
  start=$SECONDS
  if body=$(kubectl get --raw "/api/v1/namespaces/$ns/services/$svc:8000/proxy/$path"); then status=200; else status=503; fi
  ms=$(( (SECONDS - start) * 1000 + ${FAKE_LATENCY_MS:-0} ))
  slow=$(( ms > RW_SETTING_LATENCY_THRESHOLD_MS ? 1 : 0 ))
  rw_output_append probe "$(jq -nc --arg p "$path" --argjson s "$status" --argjson m "$ms" --argjson w "$slow" '{path:$p,status:$s,latencyMs:$m,slow:($w==1)}')"
  if (( status != 200 )); then
    kubectl -n "$ns" get events --field-selector "involvedObject.name=$svc,type=Warning" -o json > "$RW_OUT/events.json"
  fi
done
