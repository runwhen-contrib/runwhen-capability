source "$RW_SDK/rw.sh"
seq 1 5 | rw_artifact dump -
rw_set count '"not-an-integer"'
