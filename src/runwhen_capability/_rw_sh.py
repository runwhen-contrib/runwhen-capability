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
#
# The <json> argument must be exactly one JSON value; build it with jq (or
# printf with values you control) rather than by pasting untrusted text
# into a JSON string. Newlines in it are turned into spaces, so one call is
# always one line, and the host rejects any line that is not exactly one
# well-formed event.

set -u

rw_input() {
    # The env var name is derived exactly as the host derives it from the
    # manifest's input name (manifest.input_env_name): an underscore where a
    # lower-case letter or digit meets a capital ("maxWait" -> MAX_WAIT) and
    # before the last capital of a run followed by a lower-case letter
    # ("HTTPTimeout" -> HTTP_TIMEOUT), then upper-cased; a run of capitals is
    # one word ("THRESHOLD" -> THRESHOLD). If that variable is unset, the
    # legacy name -- an underscore before every capital but the first, the
    # only one an older host sets ("THRESHOLD" -> T_H_R_E_S_H_O_L_D) -- is
    # read instead.
    local name="${1:-}" env_name="" legacy_name="" ch prev next i
    local upper=ABCDEFGHIJKLMNOPQRSTUVWXYZ lower=abcdefghijklmnopqrstuvwxyz
    case "$name" in
        [A-Za-z]*) ;;
        *) printf 'rw_input: invalid input name %s\n' "$name" >&2; return 1 ;;
    esac
    case "$name" in
        *[!A-Za-z0-9_]*) printf 'rw_input: invalid input name %s\n' "$name" >&2; return 1 ;;
    esac
    for (( i = 0; i < ${#name}; i++ )); do
        ch=${name:i:1}
        if (( i > 0 )) && [[ $upper == *"$ch"* ]]; then
            legacy_name+="_"
            prev=${name:i-1:1}
            next=${name:i+1:1}
            if [[ $lower$'0123456789' == *"$prev"* ]]; then
                env_name+="_"
            elif [[ $upper == *"$prev"* && -n $next && $lower == *"$next"* ]]; then
                env_name+="_"
            fi
        fi
        env_name+=$ch
        legacy_name+=$ch
    done
    env_name=$(printf '%s' "$env_name" | tr "$lower" "$upper")
    legacy_name=$(printf '%s' "$legacy_name" | tr "$lower" "$upper")
    if [[ -n ${!env_name+set} ]]; then
        printf '%s' "${!env_name}"
    else
        printf '%s' "${!legacy_name-}"
    fi
}

rw_append() {
    local name="$1" value="$2"
    value=${value//$'\n'/ }
    printf '{"op":"append","name":%s,"value":%s}\n' "$(_rw_json_string "$name")" "$value" \
        >&"${RW_OUTPUT_FD}"
}

rw_set() {
    local name="$1" value="$2"
    value=${value//$'\n'/ }
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
# JSON already and never go through this.
_rw_json_string() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/\\r}
    s=${s//$'\t'/\\t}
    printf '"%s"' "$s"
}
"""
