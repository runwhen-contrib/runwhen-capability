source "$RW_SDK/rw.sh"
seq 1 5 | rw_artifact dump -
rw_skip "nothing to export"
