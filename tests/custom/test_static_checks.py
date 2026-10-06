"""static_checks's comment/heredoc stripping and the bash/kubectl scanners
that depend on it not mistaking prose for code."""

from __future__ import annotations

from runwhen_capability.custom.static_checks import (
    _strip_comments_and_heredocs,
    bash_data_as_code,
    bash_env_reads,
    bash_input_used,
    bash_locally_assigned_names,
    bash_output_writes,
    mutating_kubectl_calls,
    python_data_as_code,
    python_input_used,
    runs_kubectl,
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


def test_a_dollar_inside_single_quotes_is_not_an_env_read():
    source = (
        "jq -n --argjson total \"$COUNT\" '{total: $total}'\n"
        "jq -n '{\n  a: $a,\n  b: $b\n}'\n"
        'echo "it\'s $REAL"\n'
        "echo \\'$ESCAPED\n"
    )
    assert sorted(name for _, name in bash_env_reads(source)) == ["COUNT", "ESCAPED", "REAL"]


def test_single_quote_blanking_keeps_line_numbers():
    source = "jq '{\n a: $a\n}'\necho $LATER\n"
    assert bash_env_reads(source) == [(4, "LATER")]


def test_single_quotes_inside_a_command_substitution_in_double_quotes_are_literal():
    source = (
        'rw_set summary "$(jq -n --argjson total "$COUNT" \'{total: $total}\')"\n'
        "echo \"$(printf '%s' \"$(echo '$inner')\") $OUTER\"\n"
    )
    assert sorted(name for _, name in bash_env_reads(source)) == ["COUNT", "OUTER"]


# -- data as code: bash -------------------------------------------------------

T4_AWK = """\
DETAILS=$(awk '
  { cmd = "printf \\047%s\\047 \\047" $0 "\\047 | jq -r \\047.kind\\047"
    cmd | getline kv
    close(cmd) }' "$CAUSES_FILE")
"""


def test_an_awk_getline_built_from_data_is_flagged_on_its_own_line():
    assert bash_data_as_code(T4_AWK) == [(3, "awk | getline")]


def test_awk_system_is_flagged_even_after_an_f_option():
    text = "awk -F'|' '{ system(\"echo \" $1) }' file\n"
    assert bash_data_as_code(text) == [(1, "awk system()")]


def test_plain_awk_and_jq_arg_are_not_flagged():
    text = "awk '{ print $1 }' f\njq -r --arg k \"$K\" '.[$k]' f\n"
    assert bash_data_as_code(text) == []


def test_eval_with_an_expansion_is_flagged_but_not_a_fixed_eval():
    assert bash_data_as_code('eval "$cmd"\n') == [(1, "eval")]
    assert bash_data_as_code("eval 'echo $x'\n") == []
    assert bash_data_as_code("eval echo hi\n") == []
    assert bash_data_as_code("echo eval $x\n") == []


def test_shell_dash_c_with_an_expansion_is_flagged():
    assert bash_data_as_code('bash -c "echo $LINE"\n') == [(1, "bash -c")]
    assert bash_data_as_code('sh -c "$CMD"\n') == [(1, "sh -c")]
    assert bash_data_as_code('bash -euo pipefail -c "run $X"\n') == [(1, "bash -c")]


def test_shell_dash_c_with_a_fixed_literal_is_not_flagged():
    assert bash_data_as_code("bash -c 'echo hello'\n") == []
    assert bash_data_as_code('bash -c \'echo "$1"\' _ "$x"\n') == []
    assert bash_data_as_code('./run.sh -c "$x"\n') == []


def test_xargs_with_a_replace_string_into_sh_c_is_flagged():
    assert bash_data_as_code("ls | xargs -I{} sh -c 'echo {}'\n") == [(1, "xargs sh -c")]
    assert bash_data_as_code("ls | xargs -n1 sh -c 'echo \"$1\"' _\n") == []


def test_data_as_code_ignores_comments_and_heredocs_and_reports_each_line_once():
    text = '# eval $x\ncat <<EOF\neval $x\nEOF\neval "$a"; eval "$b"\n'
    assert bash_data_as_code(text) == [(5, "eval")]


# -- data as code: python ------------------------------------------------------


def test_python_shell_true_and_os_system_are_flagged():
    src = (
        "import os, subprocess\n"
        "def main(ctx):\n"
        "    subprocess.run(f'echo {x}', shell=True)\n"
        "    os.system('ls')\n"
        "    subprocess.check_output('ls', shell=True, text=True)\n"
    )
    assert python_data_as_code(src) == [
        (3, "subprocess.run(shell=True)"),
        (4, "os.system()"),
        (5, "subprocess.check_output(shell=True)"),
    ]


def test_python_argv_lists_and_shell_false_are_not_flagged():
    src = (
        "import subprocess\n"
        "subprocess.run(['echo', x])\n"
        "subprocess.run('ls', shell=False)\n"
        "subprocess.run(['sh', '-c', 'ls'])\n"
    )
    assert python_data_as_code(src) == []


def test_python_aliased_imports_are_followed():
    src = "import subprocess as sp\nfrom os import system\nsp.call('x', shell=True)\nsystem('y')\n"
    assert [line for line, _ in python_data_as_code(src)] == [3, 4]


def test_python_data_as_code_on_unparseable_source_is_empty():
    assert python_data_as_code("def (:\n") == []


# -- unused input ---------------------------------------------------------------


def test_bash_input_used_by_env_read_or_rw_input():
    assert bash_input_used('curl -H "Authorization: $API_TOKEN"\n', "apiToken")
    assert bash_input_used("x=${API_TOKEN:-}\n", "apiToken")
    assert bash_input_used("t=$(rw_input apiToken)\n", "apiToken")
    assert bash_input_used("printenv API_TOKEN\n", "apiToken")


def test_bash_input_not_used_when_absent_commented_or_only_a_prefix():
    assert not bash_input_used("echo hi\n", "apiToken")
    assert not bash_input_used("# uses $API_TOKEN\necho hi\n", "apiToken")
    assert not bash_input_used("echo $API_TOKEN_OTHER\n", "apiToken")


def test_bash_input_reassigned_locally_still_counts_as_read():
    assert bash_input_used('API_TOKEN="${API_TOKEN:-x}"\n', "apiToken")


def test_python_input_used_when_the_param_is_used_in_the_body():
    src = "def main(ctx, api_token, other):\n    return {'t': api_token}\n"
    assert python_input_used(src, "apiToken") is True
    assert python_input_used(src, "other") is False


def test_python_input_used_via_ctx_credential_or_kwargs_or_dynamic():
    assert python_input_used("def main(ctx):\n    ctx.credential('apiToken')\n", "apiToken")
    assert python_input_used("def main(ctx, **kw):\n    pass\n", "apiToken")
    assert python_input_used("def main(ctx, n):\n    ctx.credential(n)\n", "apiToken")
    assert python_input_used("def main(ctx):\n    pass\n", "apiToken") is False


def test_python_input_used_is_none_without_a_main():
    assert python_input_used("x = 1\n", "apiToken") is None
    assert python_input_used("def (:\n", "apiToken") is None


def test_runs_kubectl_ignores_comments():
    assert runs_kubectl("kubectl get pods\n")
    assert runs_kubectl('ctx.run(["kubectl", "get"])\n')
    assert not runs_kubectl("# kubectl get pods\necho hi\n")


def test_awk_plain_getline_and_getline_from_file_are_safe():
    assert bash_data_as_code("awk 'BEGIN { getline line; print line }' f\n") == []
    assert bash_data_as_code("awk 'BEGIN { while ((getline l < \"f\") > 0) n++ }'\n") == []


def test_awk_pipe_getline_is_flagged_without_spaces_too():
    assert bash_data_as_code("awk 'BEGIN { \"date\"|getline d }'\n") == [(1, "awk | getline")]
