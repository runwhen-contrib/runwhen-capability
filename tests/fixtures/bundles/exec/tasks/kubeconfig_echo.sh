source "$RW_SDK/rw.sh"
content=$(cat "$KUBECONFIG")
rw_set content "\"$content\""
