source "$RW_SDK/rw.sh"
name=not-declared
seq 1 3 | rw_artifact "$name" -
rw_set count 1
