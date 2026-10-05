"""What a bash task may run on the rw-task image -- the generic runtime every
custom capability's tasks execute on (rw-tasks-codecollection,
Dockerfile.rw-task). validate() warns (W_UNKNOWN_COMMAND) when a bash task
runs a command that is none of: a bash builtin or keyword, a function the
bundle defines, an `rw_*` helper, a path, a variable expansion, or a name on
RW_TASK_COMMANDS.

RW_TASK_COMMANDS is a promise, not an inventory: rw-tasks-codecollection's
image smoke test (scripts/smoke_test.py, check_runtime_commands) runs
`command -v` for every name on it inside the built image and fails the build
if one is missing. So a name goes on it only when it is in the image for a
reason that will outlive the next base-image bump:

- the image's own declared tools (scripts/apt-packages.txt, tools/tools.yaml):
  bash, curl, jq, the PostgreSQL client, redis-cli, kubectl, yq;
- Python from the python:3.12-slim base and the SDK's venv;
- openssl, a hard dependency of the pinned ca-certificates package;
- user-facing commands from Debian's Essential packages, which no Debian
  image can drop: coreutils, findutils, grep, sed, mawk (awk), gzip, tar,
  diffutils, debianutils, util-linux, bsdutils, hostname, libc-bin,
  sysvinit-utils.

Present but deliberately left off (a bundle must not come to depend on them):
perl (only there as a Debian/postgresql-client dependency), package and user
administration (apt, dpkg, useradd, mount, ...), PostgreSQL admin tools beyond
psql/pg_isready/pg_dump/pg_dumpall/pg_restore, redis-benchmark, and terminal
tools that need a tty (more, tput, clear, reset). Not in the image at all:
bc, dc, wget, nc, ping, dig, ps/procps, less, xxd, file, unzip, git, envsubst,
column.

A false warning costs an author one look; a false promise costs a broken run
in production. When unsure whether a name is in the image, leave it out.
"""

from __future__ import annotations

#: Every external command a bash task may count on finding on the rw-task
#: image's PATH. See the module docstring for what may go on it.
RW_TASK_COMMANDS: frozenset[str] = frozenset(
    {
        # -- shells
        "bash",
        "sh",
        # -- the image's declared tools (apt-packages.txt, tools.yaml)
        "curl",
        "jq",
        "kubectl",
        "yq",
        "psql",
        "pg_isready",
        "pg_dump",
        "pg_dumpall",
        "pg_restore",
        "redis-cli",
        # -- Python (python:3.12-slim base, the SDK venv)
        "python",
        "python3",
        "pip",
        "pip3",
        # -- openssl (ca-certificates depends on it)
        "openssl",
        # -- coreutils
        "arch",
        "b2sum",
        "base32",
        "base64",
        "basename",
        "basenc",
        "cat",
        "chgrp",
        "chmod",
        "chown",
        "cksum",
        "comm",
        "cp",
        "csplit",
        "cut",
        "date",
        "dd",
        "df",
        "dirname",
        "du",
        "echo",
        "env",
        "expand",
        "expr",
        "factor",
        "false",
        "fmt",
        "fold",
        "groups",
        "head",
        "id",
        "install",
        "join",
        "link",
        "ln",
        "ls",
        "md5sum",
        "mkdir",
        "mkfifo",
        "mktemp",
        "mv",
        "nice",
        "nl",
        "nohup",
        "nproc",
        "numfmt",
        "od",
        "paste",
        "pathchk",
        "pr",
        "printenv",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rm",
        "rmdir",
        "seq",
        "sha1sum",
        "sha224sum",
        "sha256sum",
        "sha384sum",
        "sha512sum",
        "shred",
        "shuf",
        "sleep",
        "sort",
        "split",
        "stat",
        "stdbuf",
        "sum",
        "sync",
        "tac",
        "tail",
        "tee",
        "test",
        "timeout",
        "touch",
        "tr",
        "true",
        "truncate",
        "tsort",
        "tty",
        "uname",
        "unexpand",
        "uniq",
        "unlink",
        "wc",
        "whoami",
        "yes",
        # -- findutils
        "find",
        "xargs",
        # -- grep, sed, mawk
        "grep",
        "egrep",
        "fgrep",
        "sed",
        "awk",
        "mawk",
        # -- gzip, tar
        "gzip",
        "gunzip",
        "zcat",
        "zgrep",
        "tar",
        # -- diffutils
        "cmp",
        "diff",
        "diff3",
        "sdiff",
        # -- debianutils, util-linux, bsdutils, hostname, libc-bin, sysvinit-utils
        "which",
        "run-parts",
        "flock",
        "getopt",
        "rev",
        "setsid",
        "logger",
        "hostname",
        "getent",
        "iconv",
        "locale",
        "pidof",
    }
)

#: A command an author commonly reaches for that is NOT on the image, mapped
#: to what to use instead. Completes "use ... or a tool listed in the
#: authoring README".
SUBSTITUTES: dict[str, str] = {
    "bc": 'awk for arithmetic (awk "BEGIN { print 3 / 4 }")',
    "dc": 'awk for arithmetic (awk "BEGIN { print 3 / 4 }")',
    "python2": "python3",
    "wget": "curl (curl -fsSL -o FILE URL)",
    "nc": "curl, or bash's /dev/tcp (exec 3<>/dev/tcp/HOST/PORT)",
    "netcat": "curl, or bash's /dev/tcp (exec 3<>/dev/tcp/HOST/PORT)",
    "telnet": "curl, or bash's /dev/tcp (exec 3<>/dev/tcp/HOST/PORT)",
    "ping": "curl, or bash's /dev/tcp, to test reachability",
    "dig": "getent hosts NAME",
    "nslookup": "getent hosts NAME",
    "host": "getent hosts NAME",
    "ps": "/proc (procps is not installed)",
    "pgrep": "/proc (procps is not installed)",
    "top": "/proc (procps is not installed)",
    "free": "/proc/meminfo (procps is not installed)",
    "xxd": "od",
    "hexdump": "od",
}

#: Bash builtins and reserved words (`compgen -b`, `compgen -k`, bash 5.2):
#: never an external command, so never checked against RW_TASK_COMMANDS.
BASH_BUILTINS_AND_KEYWORDS: frozenset[str] = frozenset(
    {
        # builtins
        "alias",
        "bg",
        "bind",
        "break",
        "builtin",
        "caller",
        "cd",
        "command",
        "compgen",
        "complete",
        "compopt",
        "continue",
        "declare",
        "dirs",
        "disown",
        "echo",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "help",
        "history",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "mapfile",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readarray",
        "readonly",
        "return",
        "set",
        "shift",
        "shopt",
        "source",
        "suspend",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
        # reserved words
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "case",
        "esac",
        "for",
        "select",
        "while",
        "until",
        "do",
        "done",
        "in",
        "function",
        "time",
        "coproc",
    }
)
