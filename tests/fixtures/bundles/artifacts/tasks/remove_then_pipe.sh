source "$RW_SDK/rw.sh"
rm -rf "$RW_ARTIFACTS_DIR"
# rw_artifact makes the directory again, which leaves it without the host's marker
seq 1 3 | rw_artifact dump -
