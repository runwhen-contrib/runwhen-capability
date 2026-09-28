"""The text of rw.sh -- kept as a Python string, not a separate packaged
resource file, so it ships in every install (editable, wheel, sdist) with no
extra packaging configuration and is written out fresh by bundle.py for each
run rather than relying on a fixed on-disk location.

bundle.py writes this to `<scope>/.rw-sdk/rw.sh` and points `RW_SDK` at its
parent directory, so a bash task does exactly what the manifest example
shows: `source "$RW_SDK/rw.sh"`.
"""

from __future__ import annotations

RW_SH = r"""# rw.sh -- sourced by a custom bundle bash task: source "$RW_SDK/rw.sh"
#
#   rw_input <name>           print input <name>'s value (its env var)
#   rw_append <output> <json> append one JSON value to a list-shaped output
#   rw_set <output> <json>    set (replace) an output's value
#   rw_skip [reason]          mark the task skipped and exit 0
#
# rw_append/rw_set/rw_skip write one JSON line to the private file
# descriptor the host opened for this task ($RW_OUTPUT_FD) -- never to
# stdout/stderr, which are logs only and never parsed for output data.

set -u

rw_input() {
    local name upper
    name="${1:-}"
    upper=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    printf '%s' "${!upper-}"
}

rw_append() {
    local name="$1" value="$2"
    printf '{"op":"append","name":%s,"value":%s}\n' "$(_rw_json_string "$name")" "$value" \
        >&"${RW_OUTPUT_FD}"
}

rw_set() {
    local name="$1" value="$2"
    printf '{"op":"set","name":%s,"value":%s}\n' "$(_rw_json_string "$name")" "$value" \
        >&"${RW_OUTPUT_FD}"
}

rw_skip() {
    local reason="${1:-}"
    printf '{"op":"skip","reason":%s}\n' "$(_rw_json_string "$reason")" >&"${RW_OUTPUT_FD}"
    exit 0
}

# Minimal JSON string escaping for a plain-text argument (an output name or
# a skip reason) -- values passed to rw_append/rw_set are the caller's own
# JSON already and are never touched here.
_rw_json_string() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    printf '"%s"' "$s"
}
"""
