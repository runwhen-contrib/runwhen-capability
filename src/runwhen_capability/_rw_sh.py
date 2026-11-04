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
#   rw_artifact <name> <path> register the file at <path> as artifact <name>
#   rw_artifact <name> -      copy stdin to $RW_ARTIFACTS_DIR/<name> and register that
#   rw_output_append <output> <json>   the same as rw_append
#   rw_output_set <output> <json>      the same as rw_set
#   rw_error <code> <message> [scope]  record a structured error and carry on
#   rw_k8s_proxy_get <namespace> <pods|services> <name> <port> <path> [--scheme https]
#                     [--timeout N]   print one JSON line {status, headers, body, truncated,
#                     location} for a GET through the Kubernetes API server's proxy that never
#                     follows a redirect
#
# rw_k8s_proxy_get does what `kubectl get --raw /api/v1/namespaces/<ns>/services/<name>:<port>/
# proxy/<path>` does -- without its flaw: kubectl follows a backend's redirect to another host
# and sends the runner's bearer token there (S17). The backend's own status and body come back
# as data (a 404 is not an error); `location` is set only for a relative redirect target, and a
# redirect to another host is dropped (`location` null, header `x-rw-redirect-dropped` names
# the host).
# On failure it records the error itself (as rw_error does, so do not call rw_error again for
# it), prints a line to stderr and returns non-zero; under `set -e` that ends the task.
#
# $RW_OUT is a directory, fresh for this task, that the task may write to freely;
# nothing in it is read by the host.
#
# $RW_NOW is the run's reference time (RFC 3339, UTC, milliseconds, e.g.
# 2026-11-04T09:14:02.123Z) and $RW_NOW_EPOCH the same instant in integer seconds: use
# them, not `date`, for anything that ends up in a command or an output, so that
# `rwtask test` replays the argv it recorded. Use $SECONDS or `date +%s` only to
# measure a duration. (Unset in a classic bundle.)
#
# rw_error records "the task could not look at something" (never a finding): <code> is
# one of permission_denied, not_found, unreachable, timeout, rate_limited, storage,
# invalid_input, dependency, internal (any other is recorded as internal, with a hint),
# <message> is one line of text, [scope] says what it was looking at. It does not exit:
# the exit code decides failed (non-zero) or not, and recorded errors make a clean
# exit partial. It appends one JSON line to the file $RW_ERRORS_FILE, which the host
# reads after the task exits.
#
# rw_append/rw_set/rw_skip/rw_artifact write one JSON line to the private file
# descriptor the host opened for this task ($RW_OUTPUT_FD) -- never to
# stdout/stderr, which are logs only and never parsed for output data.
#
# <name> must be a declared artifact output (one segment of letters, digits, "_",
# "." and "-", not starting with "." or "-"). The host uploads the file after the
# task exits, and only if the task succeeded; a file written straight into
# $RW_ARTIFACTS_DIR under a declared artifact output's name is uploaded too, with
# or without rw_artifact. Nothing is copied for <path>: the file is read where it
# is, so it must be a regular file inside the task's working directory tree, reached
# without crossing a symlink, or the task fails with E_ARTIFACT_PATH.
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

rw_output_append() { rw_append "$@"; }

rw_output_set() { rw_set "$@"; }

rw_error() {
    local code="${1:-}" message="${2:-}" scope="${3:-}" hint="" c ok=""
    if [[ -z $code || -z $message ]]; then
        printf 'rw_error: usage: rw_error <code> <message> [scope]\n' >&2
        return 2
    fi
    if [[ -z ${RW_ERRORS_FILE:-} ]]; then
        printf 'rw_error: RW_ERRORS_FILE is not set\n' >&2
        return 1
    fi
    for c in permission_denied not_found unreachable timeout rate_limited storage \
        invalid_input dependency internal; do
        [[ $code == "$c" ]] && ok=1
    done
    if [[ -z $ok ]]; then
        printf 'rw_error: %s is not an error code; recorded as internal\n' "$code" >&2
        hint="invalid error code '$code'; use one of permission_denied, not_found, unreachable, "
        hint+="timeout, rate_limited, storage, invalid_input, dependency, internal"
        code=internal
    fi
    message=${message//[[:cntrl:]]/ }
    scope=${scope//[[:cntrl:]]/ }
    local line
    line=$(printf '{"code":"%s","message":%s' "$code" "$(_rw_json_string "$message")")
    [[ -n $scope ]] && line+=$(printf ',"scope":%s' "$(_rw_json_string "$scope")")
    [[ -n $hint ]] && line+=$(printf ',"hint":%s' "$(_rw_json_string "$hint")")
    # One write of one line: appends from concurrent processes do not interleave.
    printf '%s}\n' "$line" >> "$RW_ERRORS_FILE"
}

rw_k8s_proxy_get() {
    if (( $# < 5 )); then
        printf 'rw_k8s_proxy_get: usage: rw_k8s_proxy_get <namespace> <pods|services> <name> ' >&2
        printf '<port> <path> [--scheme http|https] [--timeout seconds]\n' >&2
        return 2
    fi
    # RW_PYTHON is the interpreter that runs the task host (the host sets it); -P/-s as for
    # a Python task, so nothing in the working directory can shadow the SDK.
    "${RW_PYTHON:-python3}" -P -s -m runwhen_capability.k8s_proxy get "$@"
}

rw_skip() {
    local reason="${1:-}"
    printf '{"op":"skip","reason":%s}\n' "$(_rw_json_string "$reason")" >&"${RW_OUTPUT_FD}"
    exit 0
}

rw_artifact() {
    local name="${1:-}" src="${2:-}" path
    local word=abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
    if [[ -z $name || -z $src ]]; then
        printf 'rw_artifact: usage: rw_artifact <name> <path|->\n' >&2
        return 2
    fi
    # One segment of letters, digits, "_", "." and "-", not starting with "." or "-"
    # (the manifest's rule: custom.manifest.is_valid_artifact_name). The letters are
    # spelled out: a range like A-Z means other things in some locales.
    case "$name" in
        [!"$word"_]*|*[!"$word"_.-]*)
            printf 'rw_artifact: invalid artifact name %s\n' "$name" >&2
            return 1
            ;;
    esac
    if [[ $src == - ]]; then
        if [[ -z ${RW_ARTIFACTS_DIR:-} ]]; then
            printf 'rw_artifact: RW_ARTIFACTS_DIR is not set\n' >&2
            return 1
        fi
        path="$RW_ARTIFACTS_DIR/$name"
        mkdir -p "$RW_ARTIFACTS_DIR" || return 1
        # Streamed by cat, never held in a shell variable.
        cat > "$path" || return 1
    else
        if [[ ! -f $src || ! -r $src ]]; then
            printf 'rw_artifact: %s is not a readable file\n' "$src" >&2
            return 1
        fi
        case "$src" in
            /*) path="$src" ;;
            *) path="$PWD/$src" ;;
        esac
    fi
    printf '{"op":"artifact","name":%s,"path":%s}\n' \
        "$(_rw_json_string "$name")" "$(_rw_json_string "$path")" >&"${RW_OUTPUT_FD}"
}

# Minimal JSON string escaping for a plain-text argument (an output name, a
# skip reason or a file path) -- values passed to rw_append/rw_set are the
# caller's own JSON already and never go through this.
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


#: Read by every bash task through BASH_ENV (bundle.py sets it), before the
#: task's first line, so it applies whether or not the task sources rw.sh.
#: When the task runs a command the image doesn't have, bash calls
#: command_not_found_handle instead of only printing "command not found" and
#: carrying on: it tells the host on the private output channel, and the host
#: fails the run with E_COMMAND_NOT_FOUND. `command -v`/`type` only ask, so a
#: task can still probe for an optional tool. The name is reduced to a safe
#: alphabet so the line is always valid JSON.
BASH_ENV_SH = r"""command_not_found_handle() {
    local name="${1:0:128}"
    printf 'rw: %s: command not found on this image\n' "$name" >&2
    if [[ -n "${RW_OUTPUT_FD:-}" ]]; then
        name="${name//[^A-Za-z0-9._+-]/?}"
        printf '{"op":"missing_command","name":"%s"}\n' "$name" >&"$RW_OUTPUT_FD" 2>/dev/null
    fi
    return 127
}
"""
