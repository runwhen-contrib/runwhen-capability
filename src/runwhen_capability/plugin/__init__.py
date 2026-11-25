"""runwhen_capability.plugin -- load and validate a V13 plugin tree.

    tree = load_dir(Path("plugins/kubernetes"))      # or load_entries(archive.read(tar))
    for d in validate_tree(tree):
        print(d.file, d.path, d.code, d.message)

`load_*` never raise for a content problem (see tree.py); `validate_*` return
every `Diagnostic` (validate.py); `compile_*` produce the compiled-plugin document
(compile.py). `PluginInvalid` is what the library entry points
that need a valid plugin raise.
"""

from __future__ import annotations

from ..custom.diagnostics import Diagnostic


class PluginInvalid(ValueError):
    """A plugin has errors. `diagnostics` are the error-severity findings."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = [d for d in diagnostics if d.severity == "error"]
        shown = "; ".join(f"{d.file}: {d.code} {d.message}" for d in self.diagnostics[:5])
        more = len(self.diagnostics) - 5
        super().__init__(
            f"plugin is invalid ({len(self.diagnostics)} error(s)): {shown}"
            + (f"; and {more} more" if more > 0 else "")
        )


from .compile import compile_entries, compile_tree  # noqa: E402
from .deps import CompiledDependency, DependencyError, load_dependency  # noqa: E402
from .tree import PluginTree, SkillTree, load_dir, load_entries, load_skill_dir  # noqa: E402
from .validate import validate_entries, validate_tree  # noqa: E402

__all__ = [
    "CompiledDependency",
    "DependencyError",
    "PluginInvalid",
    "PluginTree",
    "SkillTree",
    "compile_entries",
    "compile_tree",
    "load_dependency",
    "load_dir",
    "load_entries",
    "load_skill_dir",
    "validate_entries",
    "validate_tree",
]
