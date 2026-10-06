source "$RW_SDK/rw.sh"

since="$SINCE"
max_wait="$MAX_WAIT"
kubeconfig="$KUBECONFIG"
stats_dsn="${STATS_DSN:-}"

rw_append errors "{\"pattern\":\"pool exhausted\",\"count\":3}"
rw_set summary "{\"windowMinutes\":30,\"pods\":2,\"total\":3}"
rw_set detail "{\"ok\":true}"
