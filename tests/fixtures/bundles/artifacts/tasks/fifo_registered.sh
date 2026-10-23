# rw_artifact refuses a FIFO itself; a hand-written event is what a hostile task would send
mkfifo out.fifo
printf '{"op":"artifact","name":"dump","path":"%s"}\n' "$PWD/out.fifo" >&"$RW_OUTPUT_FD"
