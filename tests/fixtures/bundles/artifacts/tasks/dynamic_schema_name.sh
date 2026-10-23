source "$RW_SDK/rw.sh"
name=count
seq 1 3 | rw_artifact "$name" -
rw_set count 1
