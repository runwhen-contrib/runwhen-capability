#!/usr/bin/env bash
ns="$RW_RESOURCE_NAMESPACE"
count=$(kubectl -n "$ns" get pods -o json | jq '.items | length')
kubectl -n "$ns" get events -o json > "$RW_OUT/events.json" || rw_error permission_denied "cannot list events" "$ns"
rw_output_set pods "$(jq -nc --arg n "$ns" --argjson c "$count" '{namespace: $n, count: $c}')"
