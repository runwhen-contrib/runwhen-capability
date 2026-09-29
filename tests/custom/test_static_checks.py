"""static_checks's comment/heredoc stripping and the bash/kubectl scanners
that depend on it not mistaking prose for code."""

from __future__ import annotations

from runwhen_capability.custom.static_checks import (
    _strip_comments_and_heredocs,
    bash_env_reads,
    bash_locally_assigned_names,
    bash_output_writes,
    mutating_kubectl_calls,
)


def test_strips_a_trailing_comment_but_keeps_the_code_before_it():
    stripped = _strip_comments_and_heredocs("rw_set x 1  # comment\n")
    assert stripped.splitlines()[0].rstrip() == "rw_set x 1"
    assert "comment" not in stripped


def test_a_hash_inside_a_quoted_string_is_not_a_comment():
    stripped = _strip_comments_and_heredocs('rw_set x "value#1"\n')
    assert 'rw_set x "value#1"' in stripped


def test_a_hash_mid_word_is_not_a_comment():
    stripped = _strip_comments_and_heredocs("echo foo#bar\n")
    assert "foo#bar" in stripped


def test_preserves_line_count_and_length():
    text = "a\nb # comment\nc\n"
    stripped = _strip_comments_and_heredocs(text)
    assert stripped.count("\n") == text.count("\n")
    assert len(stripped) == len(text)


def test_blanks_a_heredoc_body_until_its_terminator():
    text = "cat <<EOF\nrw_append x 1\nkubectl delete pod x\nEOF\nrw_set y 2\n"
    stripped = _strip_comments_and_heredocs(text)
    assert "rw_append" not in stripped
    assert "kubectl delete" not in stripped
    assert "rw_set y 2" in stripped


def test_bash_output_writes_ignores_a_comment_mentioning_rw_append():
    text = "# see rw_append errors for details\nrw_set summary 1\n"
    assert bash_output_writes(text) == [(2, "summary")]


def test_mutating_kubectl_calls_ignores_a_heredoc_example():
    text = "cat <<EOF\nkubectl delete pod x\nEOF\nkubectl get pods\n"
    assert mutating_kubectl_calls(text) == []


def test_a_default_that_reads_another_variable_reports_both_names():
    assert [name for _, name in bash_env_reads('echo "${A:-$B}"\n')] == ["A", "B"]


def test_assignments_with_and_without_a_keyword_are_local_names():
    source = "x=1\n  local y=2\nexport  Z=3\nlocalq=4\n"
    assert bash_locally_assigned_names(source) == {"x", "y", "Z", "localq"}


def test_read_and_array_builtins_bind_local_names():
    source = (
        "while IFS='|' read -ra PAT_ARRAY; do :; done\n"
        "read -r first second\n"
        "read -p 'x? ' -t 5 answer\n"
        "mapfile -t LINES < f\n"
        "readarray ROWS < f\n"
        "declare -a ITEMS\n"
        "typeset -i COUNT\n"
        "local -r K\n"
        "printf -v STAMP '%s' now\n"
        "getopts 'ab' OPT\n"
        "select CHOICE in a b; do break; done\n"
    )
    assert {
        "PAT_ARRAY",
        "first",
        "second",
        "answer",
        "LINES",
        "ROWS",
        "ITEMS",
        "COUNT",
        "K",
        "STAMP",
        "OPT",
        "CHOICE",
    } <= (bash_locally_assigned_names(source))


def test_assignments_after_a_command_separator_are_local_names():
    source = "a=1; b=2\n[ -n x ] && C=3 || D=4\nif true; then E=5; else F=6; fi\nG+=x\n"
    assert {"a", "b", "C", "D", "E", "F", "G"} <= bash_locally_assigned_names(source)


def test_a_loop_variable_from_read_is_not_an_env_read():
    source = (
        'while IFS="|" read -ra PAT_ARRAY; do\n'
        '  for P in "${PAT_ARRAY[@]}"; do echo "$P"; done\n'
        "done\n"
    )
    assert bash_env_reads(source) == []


def test_read_options_that_take_a_value_are_not_names():
    source = "read -d '' -n 3 -u 4 -p prompt VALUE\n"
    names = bash_locally_assigned_names(source)
    assert "VALUE" in names
    assert not {"prompt", "d", "n", "u", "p"} & names


def test_a_comparison_is_not_an_assignment():
    assert "X" not in bash_locally_assigned_names('[ "$X" == 1 ]\ntest "$X" = 2\n')
