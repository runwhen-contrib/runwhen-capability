"""Language-aware symbol regex for the query task's `defs`/`refs` ops. All
three functions are exercised against Python's `re` module directly -- the
same dialect grep_tree compiles patterns with (see repo_fs.py's grep_tree
docstring) -- since the query task's `defs`/`refs` ops feed a
definition_pattern()/reference_pattern() result straight into grep_tree's
`pattern` argument.
"""

from __future__ import annotations

import re

from runwhen_capability.symbols import (
    definition_pattern,
    is_definition_line,
    reference_pattern,
)

# --- definition_pattern: one alternative per supported language form -------


def test_definition_pattern_matches_python_def():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "def foo():") is not None


def test_definition_pattern_matches_python_class():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "class Foo:") is not None


def test_definition_pattern_matches_python_async_def():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "async def foo():") is not None


def test_definition_pattern_matches_js_function():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "function foo() {") is not None


def test_definition_pattern_matches_js_const():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "const foo = 5;") is not None


def test_definition_pattern_matches_js_let_and_var_too():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "let foo = 5;") is not None
    assert re.search(pattern, "var foo = 5;") is not None


def test_definition_pattern_matches_ts_interface():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "interface Foo {") is not None


def test_definition_pattern_matches_ts_type():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "type Foo = string;") is not None


def test_definition_pattern_matches_ts_enum_too():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "enum Foo {") is not None


def test_definition_pattern_matches_go_func():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "func Foo() error {") is not None


def test_definition_pattern_matches_go_method_with_a_receiver():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "func (s *Server) Foo() error {") is not None


def test_definition_pattern_matches_a_ts_typed_variable():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "export const foo: Foo = makeFoo();") is not None
    assert re.search(pattern, "let foo : number = 1;") is not None


def test_definition_pattern_ts_typed_variable_does_not_match_a_longer_name():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "const foo2: Foo = makeFoo();") is None


def test_definition_pattern_matches_a_go_generic_func():
    pattern = definition_pattern("Map")
    assert re.search(pattern, "func Map[T, U any](xs []T, f func(T) U) []U {") is not None


def test_definition_pattern_matches_a_go_method_on_a_generic_receiver():
    pattern = definition_pattern("Push")
    assert re.search(pattern, "func (s *Stack[T]) Push(v T) {") is not None


def test_definition_pattern_matches_go_type_struct():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "type Foo struct {") is not None


def test_definition_pattern_matches_go_type_interface_too():
    pattern = definition_pattern("Foo")
    assert re.search(pattern, "type Foo interface {") is not None


# --- reference-only lines are not definitions -------------------------------


def test_reference_only_line_is_not_a_definition():
    assert is_definition_line("return foo()", "foo") is False


def test_is_definition_line_true_for_an_actual_definition():
    assert is_definition_line("def foo():", "foo") is True


# --- regex metacharacters in the symbol are escaped -------------------------


def test_definition_pattern_escapes_regex_metacharacters_in_the_symbol():
    """`.` (and friends) in a caller-supplied symbol must be treated as a
    literal character, never as "any character" -- a symbol is untrusted
    input, not a pattern fragment."""
    pattern = definition_pattern("a.b")
    assert re.search(pattern, "def a.b():") is not None
    assert re.search(pattern, "def aXb():") is None


def test_reference_pattern_escapes_regex_metacharacters_in_the_symbol():
    pattern = reference_pattern("a+b")
    assert re.search(pattern, "return a+b") is not None
    assert re.search(pattern, "return aXb") is None


# --- symbol with `$` (JS identifiers may start with `$`/`_`) ---------------


def test_definition_pattern_matches_js_const_with_dollar_symbol():
    pattern = definition_pattern("$scope")
    assert re.search(pattern, "const $scope = angular.injector();") is not None


def test_reference_pattern_matches_a_dollar_prefixed_symbol():
    """Plain `\\b` never fires directly against `$` (it isn't a `\\w`
    character), so " $scope" (space, then `$`) would be invisible to a
    naive `\\bX\\b` -- both the space and `$` are non-word, so there is no
    word/non-word transition for `\\b` to anchor on."""
    pattern = reference_pattern("$scope")
    assert re.search(pattern, "const $scope = angular.injector();") is not None
    assert re.search(pattern, "$scope.foo") is not None


def test_reference_pattern_dollar_symbol_does_not_match_a_longer_identifier():
    pattern = reference_pattern("$scope")
    assert re.search(pattern, "$scopedThing") is None


# --- symbol that is a prefix of a longer identifier -------------------------


def test_definition_pattern_does_not_match_symbol_as_a_prefix_of_a_longer_name():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "def foobar():") is None


def test_reference_pattern_does_not_match_symbol_as_a_prefix_of_a_longer_name():
    pattern = reference_pattern("foo")
    assert re.search(pattern, "return foobar()") is None
    assert re.search(pattern, "return foo()") is not None


# --- indentation -------------------------------------------------------------


def test_definition_pattern_matches_an_indented_definition():
    pattern = definition_pattern("foo")
    assert re.search(pattern, "    def foo(self):") is not None
