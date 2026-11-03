"""`rwtask` -- the task host CLI:

    rwtask serve --relay <url> --pool <poolId> --workdir /work
    rwtask run <capability-dir> --request request.json [--credentials creds.json]
    rwtask run <skill>/<task> --resource PATH [--plugin-dir DIR] [--input k=v]...
        [--setting k=v]... [--identity k=v]... [--allow-write] [--dry-run] [--out DIR]
    rwtask label [--schemas] <capability-dir>
    rwtask schemas [--check] [--project-dir DIR]
    rwtask plan <dir> [--prune]
    rwtask apply <dir> -m <message> [--prune] [--adopt] [--yes]
    rwtask export <dir> [--name NAME ...] [--force]
    rwtask test <dir> [--task NAME]
    rwtask test <skill>[/<task>] [--plugin-dir DIR] [--shim TOOL]...
    rwtask test <skill>/<task> --record --resource PATH --scenario NAME [--input k=v]...
        [--setting k=v]... [--identity k=v]... [--shim TOOL]... [--force] [--allow-write]
        [--dry-run]
    rwtask port <bundle-dir> --plugin NAME --into <skills-dir> [--inventory FILE]
    rwtask validate <dir> [--with SPEC ...]
    rwtask compile <plugin-dir> [--out DIR] [--with SPEC ...]
    rwtask upconvert <src-dir> --into <skills-dir> [--name NAME] [--force]

See serve.py, run_local.py, label.py, schemas.py, gitops.py, port/ and upconvert/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from . import __version__


def _add_gitops_args(subparser: argparse.ArgumentParser) -> None:
    """--api-url/--token/--workspace, shared by plan/apply/export -- each
    falls back to an env var, mirroring --relay/--pool's RELAY_URL/POOL_ID
    pattern above."""
    subparser.add_argument(
        "--api-url",
        default=None,
        help="the RunWhen platform API base URL; default: the RW_API_URL env var",
    )
    subparser.add_argument(
        "--token",
        default=None,
        help="a bearer token for the RunWhen platform API; default: the RW_API_TOKEN env var",
    )
    subparser.add_argument(
        "--workspace",
        default=None,
        help="the workspace to act on; default: the RW_WORKSPACE env var",
    )


def _add_with_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--with",
        dest="with_specs",
        action="append",
        default=[],
        metavar="SPEC",
        help="a required (requires/extends) integration to validate against, repeatable: a "
        "plugin directory, a <name>-<version>.compiled.json file, or <name>[@<version>] from "
        "the plugin cache ($RUNWHEN_PLUGIN_CACHE, default ~/.cache/runwhen/plugins)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rwtask")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="long-poll the runner and execute requests")
    # --relay/--pool are optional on the CLI and default to RELAY_URL/POOL_ID,
    # mirroring --token-file/EXECUTOR_TOKEN_FILE. The runner launches this image
    # with no args at all -- it injects these as env vars, because a runner that
    # had to pass capability CLI flags would be encoding knowledge of the
    # capability, which the executor contract forbids. The image's own CMD
    # supplies only --capability-dir.
    serve_p.add_argument(
        "--relay",
        default=None,
        help="the runner's relay base URL; default: the RELAY_URL env var",
    )
    serve_p.add_argument(
        "--pool",
        default=None,
        dest="pool_id",
        help="this executor's pool id; default: the POOL_ID env var",
    )
    serve_p.add_argument("--workdir", default="/work", help="scope directory root (default: /work)")
    serve_p.add_argument(
        "--capability-dir",
        default=None,
        help="capability directory to serve; default: auto-discover the single "
        "capabilities/*/manifest.yaml the image ships",
    )
    serve_p.add_argument(
        "--token-file",
        default=None,
        help="path to the executor bearer token; default: the EXECUTOR_TOKEN_FILE "
        "env var, else /var/run/executor/token",
    )
    serve_p.add_argument(
        "--allow-bundles",
        action="store_true",
        default=None,
        help="also run bundle requests -- a custom capability's code carried inside the "
        "request -- and V13 leases (plugin code) instead of refusing them; default: off, "
        "unless RW_ALLOW_BUNDLES=1. Only an image built to run custom capabilities "
        "should enable this.",
    )

    run_p = sub.add_parser(
        "run",
        help="run a request against a capability directory, --local a bundle directory's "
        "task(s), or a V13 plugin task (<skill>/<task> --resource PATH), locally",
    )
    run_p.add_argument(
        "capability_dir",
        nargs="?",
        default=None,
        help="path to the capability directory (has manifest.yaml, tasks.py) -- omit with "
        "--local; or <skill>/<task> of a V13 plugin (see --resource)",
    )
    run_p.add_argument("--request", default=None, help="path to a request.json (a RequestEnvelope)")
    run_p.add_argument(
        "--local",
        default=None,
        metavar="DIR",
        help="run a custom capability bundle directory (has capability.yaml, tasks/, ...) "
        "instead of a packaged capability -- the same code path bundle mode uses in "
        "`rwtask serve`. Combine with --task/--inputs, not --request.",
    )
    run_p.add_argument(
        "--task",
        dest="tasks",
        action="append",
        default=None,
        metavar="NAME",
        help="a bundle task to run (repeatable); required with --local",
    )
    run_p.add_argument(
        "--inputs",
        default=None,
        metavar="JSON",
        help="the bundle's runtime inputs, as a JSON object; --local only",
    )
    run_p.add_argument(
        "--target",
        default=None,
        metavar="JSON",
        help="the run's target resource, as a JSON object; --local only",
    )
    run_p.add_argument(
        "--credentials", default=None, help="path to a credentials.json ({name: value})"
    )
    run_p.add_argument(
        "--workdir", default=None, help="scope directory to use; default: a fresh temp dir"
    )
    run_p.add_argument(
        "--keep-workdir",
        action="store_true",
        help="don't delete the scope directory after the request completes",
    )
    run_p.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="degrade every unresolved credential to anonymous instead of failing the "
        "request -- a deliberate local-dev escape hatch for testing against public "
        "repos without a credentials.json; never available to `rwtask serve`",
    )

    v13_group = run_p.add_argument_group(
        "V13 plugin task",
        "`rwtask run <skill>/<task> --resource PATH`: your own environment (KUBECONFIG, PG*, "
        "AWS_*, ~/.kube) is the credential (D43). The result envelope is written to "
        "<out>/result.yaml and any artifacts to <out>/<name>; exit 1 only if it is `failed`.",
    )
    v13_group.add_argument(
        "--resource",
        default=None,
        metavar="PATH",
        help="the target's resource path, e.g. kubernetes/clusters/kind/namespaces/web/"
        "deployments/api",
    )
    v13_group.add_argument(
        "--plugin-dir", default=".", metavar="DIR", help="the plugin directory; default: ."
    )
    v13_group.add_argument(
        "--input",
        dest="v13_inputs",
        action="append",
        default=None,
        metavar="K=V",
        help="a task input (repeatable); JSON for integer/number/boolean/array/object types",
    )
    v13_group.add_argument(
        "--setting",
        dest="v13_settings",
        action="append",
        default=None,
        metavar="K=V",
        help="a task setting over its default (repeatable); typed like --input",
    )
    v13_group.add_argument(
        "--identity",
        dest="v13_identity",
        action="append",
        default=None,
        metavar="K=V",
        help="set or override an identity field of the target (repeatable)",
    )
    v13_group.add_argument(
        "--allow-write",
        action="store_true",
        help="run a task that is not read-only (readOnly: false)",
    )
    v13_group.add_argument(
        "--dry-run",
        action="store_true",
        help="set RW_DRY_RUN=1; only for a task that declares dryRun: true",
    )
    v13_group.add_argument(
        "--out", default="out", metavar="DIR", help="where to write result.yaml; default: ./out"
    )

    label_p = sub.add_parser(
        "label",
        help="print a capability's OCI label value (base64) on stdout, for an image build arg",
    )
    label_p.add_argument("capability_dir", help="path to the capability directory")
    label_p.add_argument(
        "--schemas",
        action="store_true",
        help="print the com.runwhen.capability.schemas.v1 value (every file under schemas/) "
        "instead of com.runwhen.capability.manifest.v1 (manifest.yaml)",
    )

    schemas_p = sub.add_parser(
        "schemas",
        help="export the output schemas listed under [tool.rwtask.schemas] in pyproject.toml",
    )
    schemas_p.add_argument(
        "--check",
        action="store_true",
        help="write nothing; fail if a listed schema file is missing or out of date",
    )
    schemas_p.add_argument(
        "--project-dir",
        default=".",
        help="directory holding pyproject.toml (default: the current directory)",
    )

    plan_p = sub.add_parser(
        "plan",
        help="validate every capability.yaml under a directory and show what `apply` would "
        "change, Terraform-style: exit 0 (no changes), 2 (changes) or 1 (error)",
    )
    plan_p.add_argument("dir", help="root directory to search for capability.yaml bundles")
    plan_p.add_argument(
        "--prune",
        action="store_true",
        help="also plan removing every git-managed capability missing from dir",
    )
    _add_gitops_args(plan_p)

    apply_p = sub.add_parser(
        "apply", help="plan, then publish every capability.yaml under a directory"
    )
    apply_p.add_argument("dir", help="root directory to search for capability.yaml bundles")
    apply_p.add_argument(
        "-m",
        "--message",
        required=True,
        help="the commit-style message the published version records",
    )
    apply_p.add_argument(
        "--prune",
        action="store_true",
        help="also soft-delete every git-managed capability missing from dir",
    )
    apply_p.add_argument(
        "--adopt",
        action="store_true",
        help="take over a capability that already exists but is not git-managed",
    )
    apply_p.add_argument(
        "--yes", action="store_true", help="apply without an interactive confirmation prompt"
    )
    _add_gitops_args(apply_p)

    export_p = sub.add_parser(
        "export", help="write a workspace's published custom capabilities to disk"
    )
    export_p.add_argument("dir", help="directory to export into (one subdirectory per capability)")
    export_p.add_argument(
        "--name",
        dest="names",
        action="append",
        default=None,
        metavar="NAME",
        help="export only this capability (repeatable); default: every capability in the workspace",
    )
    export_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite a local file that has changed, instead of refusing to",
    )
    _add_gitops_args(export_p)

    test_p = sub.add_parser(
        "test",
        help="run each capability's tests/<task>.json locally, the same way run --local does; "
        "or replay (--record: record) a V13 skill's CLI-call fixtures (<skill>[/<task>])",
    )
    test_p.add_argument(
        "dir",
        help="root directory to search for capability.yaml bundles -- or <skill>[/<task>] of "
        "a V13 plugin (see --plugin-dir), whose fixtures are skills/<skill>/tests/<task>/*.yaml",
    )
    test_p.add_argument(
        "--task",
        default=None,
        metavar="NAME",
        help="run only the test for this task name, across every capability that has one",
    )
    v13_test = test_p.add_argument_group(
        "V13 skill fixtures",
        "Without --record every scenario is replayed: kubectl, helm, flux, argocd, git, psql, "
        "redis-cli, curl, az, gcloud and aws (and any --shim TOOL) answer from the recorded "
        "calls, in order, and the envelope is checked against `expect`. --record runs the task "
        "once for real, with your own environment as the credential (D43), and writes the "
        "scenario. Review a recorded file before committing it: it holds what the tools printed.",
    )
    v13_test.add_argument(
        "--plugin-dir", default=None, metavar="DIR", help="the plugin directory; default: ."
    )
    v13_test.add_argument(
        "--shim",
        dest="shims",
        action="append",
        default=None,
        metavar="TOOL",
        help="also shim this tool (repeatable); give it to replay as it was given to --record",
    )
    v13_test.add_argument(
        "--record", action="store_true", help="run the task for real and write its scenario"
    )
    v13_test.add_argument(
        "--scenario", default=None, metavar="NAME", help="--record: the scenario's file name"
    )
    v13_test.add_argument(
        "--resource", default=None, metavar="PATH", help="--record: the target's resource path"
    )
    v13_test.add_argument(
        "--input",
        dest="v13_inputs",
        action="append",
        default=None,
        metavar="K=V",
        help="--record: a task input (repeatable); typed like `rwtask run --input`",
    )
    v13_test.add_argument(
        "--setting",
        dest="v13_settings",
        action="append",
        default=None,
        metavar="K=V",
        help="--record: a task setting (repeatable)",
    )
    v13_test.add_argument(
        "--identity",
        dest="v13_identity",
        action="append",
        default=None,
        metavar="K=V",
        help="--record: set or override an identity field of the target (repeatable)",
    )
    v13_test.add_argument(
        "--force", action="store_true", help="--record: overwrite an existing scenario file"
    )
    v13_test.add_argument(
        "--allow-write",
        action="store_true",
        help="--record: run a task that is not read-only (it really runs)",
    )
    v13_test.add_argument(
        "--dry-run",
        action="store_true",
        help="--record: run with RW_DRY_RUN=1 and write `dryRun: true`; only for a task that "
        "declares dryRun: true. Needs no --allow-write",
    )

    port_p = sub.add_parser(
        "port",
        help="draft a V13 skill and a porting brief from a classic rw-cli Robot bundle",
    )
    port_p.add_argument("bundle_dir", help="the classic codebundle directory")
    port_p.add_argument("--plugin", required=True, help="the plugin the skill is for")
    port_p.add_argument(
        "--inventory",
        default=None,
        metavar="FILE",
        help="a YAML list of bundle inventory entries (asserts, readOnly, permissions, ...)",
    )
    port_p.add_argument("--skill", default=None, help="the skill name; default: the bundle name")
    port_p.add_argument(
        "--into", required=True, help="the plugin's skills directory; writes <into>/<skill>/"
    )
    port_p.add_argument(
        "--brief",
        default=None,
        help="where to write the porting brief; default: <into>/../porting/briefs/<bundle>.md",
    )
    port_p.add_argument("--force", action="store_true", help="overwrite files that already exist")

    validate_p = sub.add_parser(
        "validate",
        help="validate a V13 plugin directory (has plugin.yaml) or one skill directory "
        "(has SKILL.md); prints file:path: CODE message, exit 1 on any error",
    )
    validate_p.add_argument("dir", help="plugin directory, or a skill directory")
    _add_with_arg(validate_p)

    compile_p = sub.add_parser(
        "compile",
        help="validate a V13 plugin directory, then write <out>/<name>-<version>.tar and "
        ".compiled.json and print the archive's sha256",
    )
    compile_p.add_argument("dir", help="plugin directory (has plugin.yaml)")
    _add_with_arg(compile_p)
    compile_p.add_argument(
        "--out", default="dist", help="directory to write the archive and compiled.json into"
    )

    upconvert_p = sub.add_parser(
        "upconvert",
        help="up-convert a custom-capability/v1 bundle or a capability/v1 directory to a V13 "
        "skill at <into>/<name>/; prints the up-conversion findings, then validates the result",
    )
    upconvert_p.add_argument(
        "src_dir", help="the classic capability directory (capability.yaml or manifest.yaml)"
    )
    upconvert_p.add_argument(
        "--into", required=True, help="the skills directory; writes <into>/<name>/"
    )
    upconvert_p.add_argument("--name", default=None, help="the skill name; default: the source's")
    upconvert_p.add_argument(
        "--force", action="store_true", help="replace <into>/<name>/ if it already exists"
    )

    return parser


def _upconvert(args: argparse.Namespace) -> int:
    import shutil

    from .plugin import load_skill_dir, validate_tree
    from .upconvert import UpconvertError, upconvert_dir, write_result

    try:
        result = upconvert_dir(Path(args.src_dir), name=args.name)
    except UpconvertError as exc:
        print(f"rwtask upconvert: {exc}", file=sys.stderr)
        return 1
    if (
        result.skill_name in ("", ".", "..")
        or "/" in result.skill_name
        or "\\" in result.skill_name
    ):
        print(
            f"rwtask upconvert: {result.skill_name!r} is not a usable skill name", file=sys.stderr
        )
        return 1
    target = Path(args.into) / result.skill_name
    if target.exists() or target.is_symlink():
        if not args.force:
            print(
                f"rwtask upconvert: {target} already exists; use --force to replace it",
                file=sys.stderr,
            )
            return 1
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    write_result(result, target)
    diagnostics = [*result.diagnostics, *validate_tree(load_skill_dir(target))]
    for diag in diagnostics:
        print(_format_plugin_diagnostic(diag))
    errors = sum(d.severity == "error" for d in diagnostics)
    warnings = len(diagnostics) - errors
    print(
        f"rwtask upconvert: wrote {target}: {errors} error(s), {warnings} warning(s)",
        file=sys.stderr,
    )
    return 1 if errors else 0


def _port(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    import yaml

    from .port.draft import draft_skill, load_inventory_entry
    from .port.robot_model import PortDependencyError, read_bundle

    bundle_dir = Path(args.bundle_dir)
    if not bundle_dir.is_dir():
        print(f"rwtask port: {bundle_dir} is not a directory", file=sys.stderr)
        return 1
    try:
        bundle = read_bundle(bundle_dir)
        inventory = None
        if args.inventory:
            inventory = load_inventory_entry(Path(args.inventory), bundle.name)
        draft = draft_skill(
            bundle,
            args.plugin,
            inventory=inventory,
            skill_name=args.skill,
            inventory_missing=bool(args.inventory) and inventory is None,
        )
    except PortDependencyError as exc:
        print(f"rwtask port: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"rwtask port: {exc}", file=sys.stderr)
        return 1

    into = Path(args.into).resolve()
    skill_dir = into / draft.skill_name
    if args.brief:
        brief_path = Path(args.brief).resolve()
    else:
        brief_path = into.parent / "porting" / "briefs" / f"{bundle.name}.md"
    if brief_path == skill_dir or skill_dir in brief_path.parents:
        print(
            f"rwtask port: the brief must live outside the skill directory {skill_dir}",
            file=sys.stderr,
        )
        return 1

    targets = {skill_dir / name: text for name, text in draft.files.items()}
    targets[brief_path] = draft.brief
    existing = [str(p) for p in targets if p.exists()]
    if existing and not args.force:
        print(
            "rwtask port: refusing to overwrite " + ", ".join(existing) + "; use --force",
            file=sys.stderr,
        )
        return 1
    for path, text in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    for warning in draft.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _load_dependencies(who: str, specs: list[str]):
    """{plugin name: CompiledDependency} for the `--with` specs, or None after printing
    why one did not resolve (the caller exits 2)."""
    from .plugin import DependencyError, load_dependency

    dependencies = {}
    for spec in specs:
        try:
            dep = load_dependency(spec)
        except DependencyError as exc:
            print(f"{who}: --with {spec}: {exc}", file=sys.stderr)
            return None
        if dep.name in dependencies:
            print(f"{who}: --with names {dep.name!r} twice", file=sys.stderr)
            return None
        dependencies[dep.name] = dep
    return dependencies


def _format_plugin_diagnostic(diag) -> str:
    where = diag.file or ""
    if diag.path:
        where = f"{where}:{diag.path}"
    prefix = "warning: " if diag.severity == "warning" else ""
    head = f"{where}: " if where else ""
    return f"{prefix}{head}{diag.code} {diag.message}"


def _is_v13_run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> bool:
    """`rwtask run <skill>/<task>`: the first positional has a `/` and is not an existing
    directory (a classic `rwtask run path/to/capability` keeps working), or `--resource`
    says so. `--local` is always the classic bundle form."""
    target = args.capability_dir
    if args.local or not target:
        return False
    if args.resource is not None:
        if "/" not in target:
            parser.error("run --resource needs <skill>/<task>, got " + repr(target))
        return True
    return "/" in target and not Path(target).is_dir()


def _key_values(parser: argparse.ArgumentParser, flag: str, items: list[str] | None):
    pairs = []
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"{flag} expects K=V, got {item!r}")
        pairs.append((key, value))
    return pairs


def _load_compiled_plugin(who: str, directory: Path):
    """(tree, compiled plugin) for the plugin in `directory`, or the exit code (1) after
    printing why: no plugin.yaml, a validation error, or a plugin that does not compile.
    Warnings are printed and do not stop anything."""
    from .plugin import PluginInvalid, compile_tree, load_dir, validate_tree

    if not (directory / "plugin.yaml").is_file():
        print(f"{who}: {directory} has no plugin.yaml (see --plugin-dir)", file=sys.stderr)
        return 1
    tree = load_dir(directory)
    diagnostics = validate_tree(tree)
    for diag in diagnostics:
        print(_format_plugin_diagnostic(diag), file=sys.stderr)
    if any(d.severity == "error" for d in diagnostics):
        return 1
    try:
        return tree, compile_tree(tree)
    except PluginInvalid as exc:
        print(f"{who}: {exc}", file=sys.stderr)
        return 1


def _find_task(who: str, compiled: dict, ref: str) -> dict | None:
    """The compiled task `[<plugin>:]<skill>/<task>`, or None after printing the tasks there are."""
    wanted = ref.split(":", 1)[1] if ":" in ref else ref
    tasks = {f"{t['skill']}/{t['name']}": t for t in compiled["tasks"]}
    task = tasks.get(wanted)
    if task is None:
        available = ", ".join(sorted(tasks)) or "none"
        print(f"{who}: no task {wanted!r} in this plugin (tasks: {available})", file=sys.stderr)
    return task


def _typed_values(
    parser: argparse.ArgumentParser,
    flag: str,
    items: list[str] | None,
    specs: dict | None,
    bad_values: list[str],
) -> dict:
    """`--input`/`--setting` K=V items typed by the task's declared types; a value that is
    not JSON for its type is added to `bad_values` instead."""
    from .plugin.run import coerce_value

    values = {}
    for key, raw in _key_values(parser, flag, items):
        try:
            values[key] = coerce_value(((specs or {}).get(key) or {}).get("type"), raw)
        except ValueError as exc:
            bad_values.append(f"{flag} {key}: {exc}")
    return values


def _target_for(
    who: str, ref: str, task: dict, tree, resource: str | None, identity: dict
) -> tuple[dict | None, int | None]:
    """(the run's target, None), or (None, the exit code) after printing why not. Warns,
    without stopping, when the task applies to other types than the resource's."""
    from .plugin.run import resolve_target

    if resource is None:
        if task.get("appliesTo"):
            print(f"{who}: {ref} needs a target: pass --resource PATH", file=sys.stderr)
            return None, 2
        return None, None
    try:
        target = resolve_target(resource, tree, identity_overrides=identity)
    except ValueError as exc:
        print(f"{who}: {exc}", file=sys.stderr)
        return None, 2
    for warning in target.pop("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    applies = (task.get("appliesTo") or {}).get("types")
    if applies and target["type"] not in applies:
        print(
            f"warning: {ref} applies to {', '.join(applies)}, not {target['type']}",
            file=sys.stderr,
        )
    return target, None


def _run_v13(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    import shutil
    import tempfile

    import yaml

    from .plugin.run import RunSpec, execute

    ref = args.capability_dir
    if args.request or args.tasks or args.inputs or args.target or args.credentials:
        parser.error(
            "run <skill>/<task> takes none of --request/--task/--inputs/--target/--credentials"
        )
    loaded = _load_compiled_plugin("rwtask run", Path(args.plugin_dir))
    if isinstance(loaded, int):
        return loaded
    tree, compiled = loaded
    task = _find_task("rwtask run", compiled, ref)
    if task is None:
        return 2
    if not task["readOnly"] and not (args.allow_write or (args.dry_run and task["dryRun"])):
        effects = "; ".join(task.get("effects") or ["changes things"])
        hint = ", or --dry-run to only look" if task["dryRun"] else ""
        print(
            f"rwtask run: {ref} is not read-only: it {effects}. Pass --allow-write to run it{hint}",
            file=sys.stderr,
        )
        return 2
    if args.dry_run and not task["dryRun"]:
        print(
            f"rwtask run: {ref} does not declare dryRun: true, so --dry-run is refused",
            file=sys.stderr,
        )
        return 2

    bad_values: list[str] = []
    inputs = _typed_values(parser, "--input", args.v13_inputs, task.get("inputs"), bad_values)
    settings = _typed_values(
        parser, "--setting", args.v13_settings, task.get("settings"), bad_values
    )
    if bad_values:
        for message in bad_values:
            print(f"rwtask run: {message}", file=sys.stderr)
        return 2
    identity = dict(_key_values(parser, "--identity", args.v13_identity))

    target, failure = _target_for("rwtask run", ref, task, tree, args.resource, identity)
    if failure is not None:
        return failure

    spec = RunSpec(
        task=task,
        files=tree.files,
        executable=tree.executable,
        target=target,
        inputs=inputs,
        settings=settings,
        dry_run=args.dry_run,
    )
    owns_workdir = args.workdir is None
    scope_dir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="rwtask-run-"))
    out = Path(args.out)
    try:
        outcome = execute(spec, scope_dir=scope_dir, inherit_home=True)
        out.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(outcome.envelope, sort_keys=False, allow_unicode=True)
        (out / "result.yaml").write_text(text, encoding="utf-8")
        declared = [n for n, o in (task.get("outputs") or {}).items() if "artifact" in o]
        for name in declared:  # never leave a stale file from an earlier run to pass for this one
            if name not in outcome.artifacts:
                (out / name).unlink(missing_ok=True)
        for name, source in outcome.artifacts.items():
            (out / name).unlink(missing_ok=True)
            shutil.copyfile(source, out / name)
    finally:
        if owns_workdir and not args.keep_workdir:
            shutil.rmtree(scope_dir, ignore_errors=True)
        else:
            print(f"rwtask run: scope kept at {scope_dir}", file=sys.stderr)

    envelope = outcome.envelope
    summary = f"rwtask run: {ref}: {envelope['status']}"
    if envelope.get("reason"):
        summary += f" ({envelope['reason']})"
    if envelope["errors"]:
        summary += " [" + ", ".join(e["code"] for e in envelope["errors"]) + "]"
    print(summary + f" -> {out / 'result.yaml'}", file=sys.stderr)
    for error in envelope["errors"]:
        print(f"  {error['code']}: {error['message']}", file=sys.stderr)
    if envelope["status"] in ("failed", "partial") and outcome.log_tail.strip():
        print("--- output of the script ---", file=sys.stderr)
        print(outcome.log_tail.rstrip(), file=sys.stderr)
    return 1 if envelope["status"] == "failed" else 0


_SCENARIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RECORD_HEADER = (
    "# Recorded by `rwtask test --record`. Review it before committing: `calls` holds what the\n"
    "# tools printed. `expect.outputs` is left for you to add -- `outputs: {name: value}` is a\n"
    "# subset match (a dict by key, a list or a scalar whole; `{$contains: [..]}` and\n"
    "# `{$containsWhere: {..}}` look inside a list). `expect.artifacts` does the same for\n"
    "# artifact files. `rwtask test` replays this file."
)


def _v13_test_flags(args: argparse.Namespace) -> bool:
    return bool(
        args.record
        or args.plugin_dir is not None
        or args.resource is not None
        or args.v13_inputs
        or args.v13_settings
        or args.v13_identity
        or args.shims
        or args.scenario is not None
        or args.force
        or args.allow_write
        or args.dry_run
    )


def _is_v13_test(args: argparse.Namespace) -> bool:
    """`rwtask test <skill>[/<task>]`: any V13 flag, or the first path segment names a
    skill under `<plugin-dir>/skills/`, or the positional has a `/` and is not an existing
    directory (a classic `rwtask test path/to/capabilities` keeps working)."""
    if _v13_test_flags(args):
        return True
    target = args.dir.split(":", 1)[-1]
    if (Path(args.plugin_dir or ".") / "skills" / target.split("/", 1)[0]).is_dir():
        return True
    return "/" in args.dir and not Path(args.dir).is_dir()


def _test_v13(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    who = "rwtask test --record" if args.record else "rwtask test"
    if args.task:
        parser.error("test <skill>[/<task>] has no --task: name the task as <skill>/<task>")
    if not args.record:
        used = [
            flag
            for flag, value in (
                ("--scenario", args.scenario),
                ("--resource", args.resource),
                ("--input", args.v13_inputs),
                ("--setting", args.v13_settings),
                ("--identity", args.v13_identity),
                ("--force", args.force),
                ("--allow-write", args.allow_write),
                ("--dry-run", args.dry_run),
            )
            if value
        ]
        if used:
            parser.error(f"{', '.join(used)} only apply with --record")
    from .plugin.shims import DEFAULT_SHIMS, check_tool_name

    try:
        for tool in args.shims or []:
            check_tool_name(tool)
    except ValueError as exc:
        parser.error(str(exc))
    shims = list(dict.fromkeys([*DEFAULT_SHIMS, *(args.shims or [])]))

    skill, _, task_name = args.dir.split(":", 1)[-1].partition("/")
    directory = Path(args.plugin_dir or ".")
    if args.record and not task_name:
        print(f"{who}: --record needs <skill>/<task>, got {args.dir!r}", file=sys.stderr)
        return 2
    loaded = _load_compiled_plugin(who, directory)
    if isinstance(loaded, int):
        return loaded
    tree, compiled = loaded
    skill_tasks = {t["name"]: t for t in compiled["tasks"] if t["skill"] == skill}
    if not skill_tasks:
        skills = ", ".join(sorted({t["skill"] for t in compiled["tasks"]})) or "none"
        print(
            f"{who}: no skill {skill!r} with tasks in this plugin (skills: {skills})",
            file=sys.stderr,
        )
        return 2
    tests_root = directory / "skills" / skill / "tests"
    if args.record:
        task = skill_tasks.get(task_name)
        if task is None:
            _find_task(who, compiled, f"{skill}/{task_name}")
            return 2
        return _record_v13(parser, args, tree, task, tests_root, shims)
    if task_name and task_name not in skill_tasks:
        _find_task(who, compiled, f"{skill}/{task_name}")
        return 2
    return _replay_v13(args, tree, skill, skill_tasks, task_name, tests_root, shims)


def _record_v13(parser, args, tree, task: dict, tests_root: Path, shims: list[str]) -> int:
    import shutil
    import tempfile

    from .plugin.fixtures import dump_scenario
    from .plugin.run import RunSpec, execute
    from .plugin.shims import ShimSession

    who = "rwtask test --record"
    ref = f"{task['skill']}/{task['name']}"
    if not args.scenario or not _SCENARIO_NAME.match(args.scenario):
        print(
            f"{who}: --scenario NAME is required (letters, digits, '.', '_', '-'; "
            f"got {args.scenario!r})",
            file=sys.stderr,
        )
        return 2
    destination = tests_root / task["name"] / f"{args.scenario}.yaml"
    if destination.exists() and not args.force:
        print(f"{who}: {destination} already exists; pass --force to overwrite it", file=sys.stderr)
        return 2
    if args.dry_run and not task["dryRun"]:
        print(
            f"{who}: {ref} does not declare dryRun: true, so --dry-run is refused", file=sys.stderr
        )
        return 2
    if not task["readOnly"] and not (args.allow_write or (args.dry_run and task["dryRun"])):
        effects = "; ".join(task.get("effects") or ["changes things"])
        hint = ", or --dry-run to only look" if task["dryRun"] else ""
        print(
            f"{who}: {ref} is not read-only: it {effects}. Recording really runs it; "
            f"pass --allow-write to record it{hint}",
            file=sys.stderr,
        )
        return 2

    bad_values: list[str] = []
    inputs = _typed_values(parser, "--input", args.v13_inputs, task.get("inputs"), bad_values)
    settings = _typed_values(
        parser, "--setting", args.v13_settings, task.get("settings"), bad_values
    )
    if bad_values:
        for message in bad_values:
            print(f"{who}: {message}", file=sys.stderr)
        return 2
    identity = dict(_key_values(parser, "--identity", args.v13_identity))
    target, failure = _target_for(who, ref, task, tree, args.resource, identity)
    if failure is not None:
        return failure

    scope_dir = Path(tempfile.mkdtemp(prefix="rwtask-test-"))
    try:
        with ShimSession.record(shims) as session:
            for tool in session.missing:
                if tool in (args.shims or []):
                    print(f"warning: --shim {tool}: not installed, so not shimmed", file=sys.stderr)
            outcome = execute(
                RunSpec(
                    task=task,
                    files=tree.files,
                    executable=tree.executable,
                    target=target,
                    inputs=inputs,
                    settings=settings,
                    dry_run=args.dry_run,
                ),
                scope_dir=scope_dir,
                inherit_home=True,
                path_prepend=session.path_prepend,
                shim_env=session.env,
            )
            calls = session.recorded()
    finally:
        shutil.rmtree(scope_dir, ignore_errors=True)

    envelope = outcome.envelope
    expect = {
        "status": envelope["status"],
        "errorCodes": sorted({e["code"] for e in envelope["errors"]}),
    }
    text = dump_scenario(
        resource=args.resource,
        inputs=inputs,
        settings=settings,
        identity=identity,
        calls=calls,
        expect=expect,
        header=_RECORD_HEADER,
        dry_run=args.dry_run,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print(
        f"{who}: recorded {len(calls)} calls, status {envelope['status']}"
        + (f" [{', '.join(expect['errorCodes'])}]" if expect["errorCodes"] else "")
        + f" -> {destination}",
        file=sys.stderr,
    )
    for error in envelope["errors"]:
        print(f"  {error['code']}: {error['message']}", file=sys.stderr)
    return 0


def _replay_v13(
    args, tree, skill: str, skill_tasks: dict, task_name: str, tests_root: Path, shims: list[str]
) -> int:
    import shutil
    import tempfile

    from .plugin.fixtures import FixtureError, load_scenario, match_expect, scenario_files
    from .plugin.run import RunSpec, execute
    from .plugin.shims import ShimSession

    who = "rwtask test"
    passed = failed = 0
    if task_name:
        names = [task_name]
    else:
        # every task with fixtures, plus any fixture directory no task answers to
        on_disk = (
            sorted(p.name for p in tests_root.iterdir() if p.is_dir())
            if tests_root.is_dir()
            else []
        )
        names = sorted({*(n for n in skill_tasks if n in on_disk), *on_disk})
    for name in names:
        task = skill_tasks.get(name)
        files = scenario_files(tests_root / name)
        if task is None:
            if files:
                failed += 1
                print(f"FAIL {skill}/{name}: no such task (fixtures in {tests_root / name})")
            continue
        for path in files:
            label = f"{skill}/{name}/{path.stem}"
            try:
                scenario = load_scenario(path)
            except FixtureError as exc:
                failed += 1
                print(f"FAIL {label}")
                for problem in exc.errors:
                    print(f"  {problem}")
                continue
            problems: list[str] = []
            if scenario.dry_run and not task["dryRun"]:
                failed += 1
                print(f"FAIL {label}")
                print(
                    "  the scenario says dryRun: true, but the task does not declare dryRun: true"
                )
                continue
            target, failure = _target_for(
                who, f"{skill}/{name}", task, tree, scenario.resource, scenario.identity
            )
            if failure is not None:
                failed += 1
                print(f"FAIL {label}")
                print("  the scenario's resource/identity cannot be used (see the message above)")
                continue
            scope_dir = Path(tempfile.mkdtemp(prefix="rwtask-test-"))
            try:
                with ShimSession.replay(scenario.calls, shims) as session:
                    outcome = execute(
                        RunSpec(
                            task=task,
                            files=tree.files,
                            executable=tree.executable,
                            target=target,
                            inputs=scenario.inputs,
                            settings=scenario.settings,
                            dry_run=scenario.dry_run,
                        ),
                        scope_dir=scope_dir,
                        path_prepend=session.path_prepend,
                        shim_env=session.env,
                    )
                    mismatches = session.mismatches()
                    unconsumed = session.unconsumed()
                # the artifact files go with the scope directory: read them now
                artifact_texts = {
                    artifact: file.read_text(encoding="utf-8", errors="replace")
                    for artifact, file in outcome.artifacts.items()
                }
            finally:
                shutil.rmtree(scope_dir, ignore_errors=True)
            if mismatches:
                problems.extend(
                    f"E_FIXTURE_MISMATCH call {m['index'] + 1}: {m['message']}" for m in mismatches
                )
            else:
                problems.extend(
                    match_expect(
                        outcome.envelope,
                        scenario.expect,
                        artifacts=artifact_texts,
                        artifact_outputs=[
                            n for n, o in (task.get("outputs") or {}).items() if "artifact" in o
                        ],
                    )
                )
                if unconsumed:
                    problems.append(f"{unconsumed} recorded calls not made")
            if problems:
                failed += 1
                print(f"FAIL {label}")
                for problem in problems:
                    print(f"  {problem}")
                if outcome.envelope["status"] in ("failed", "partial") and outcome.log_tail.strip():
                    print("  --- output of the script ---")
                    for line in outcome.log_tail.rstrip().splitlines():
                        print(f"  {line}")
            else:
                passed += 1
                print(f"PASS {label}")
    if not passed and not failed:
        scope = f"{skill}/{task_name}" if task_name else skill
        print(f"{who}: no fixtures for {scope} (looked in {tests_root}/<task>/*.yaml)")
        return 1
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


def _gitops_config(parser: argparse.ArgumentParser, args: argparse.Namespace, command: str):
    from . import gitops

    api_url = args.api_url or os.environ.get("RW_API_URL")
    token = args.token or os.environ.get("RW_API_TOKEN")
    workspace = args.workspace or os.environ.get("RW_WORKSPACE")
    missing = [
        name
        for name, value in (
            ("--api-url/RW_API_URL", api_url),
            ("--token/RW_API_TOKEN", token),
            ("--workspace/RW_WORKSPACE", workspace),
        )
        if not value
    ]
    if missing:
        parser.error(f"{command} needs " + " and ".join(missing))
    return gitops.GitOpsConfig(api_url=api_url, token=token, workspace=workspace)


def _print_plan(capabilities: list[dict]) -> None:
    from . import gitops

    for cap in capabilities:
        print(f"{cap.get('name')}: {cap.get('action')}")
        for diag in cap.get("diagnostics") or []:
            print(
                "  "
                + gitops.format_diagnostic(
                    diag.get("code", ""),
                    diag.get("message", ""),
                    diag.get("file"),
                    diag.get("line"),
                )
            )
        for entry in cap.get("files") or []:
            print(f"  {entry.get('change')} {entry.get('path')}")
            diff = entry.get("diff")
            if diff:
                for line in diff.rstrip("\n").splitlines():
                    print(f"    {line}")


def _print_local_validation_errors(invalid) -> None:
    from . import gitops

    for capability_dir, diagnostics in invalid:
        for diag in diagnostics:
            if diag.severity != "error":
                continue
            file = f"{capability_dir}/{diag.file}" if diag.file else str(capability_dir)
            print(
                gitops.format_diagnostic(diag.code, diag.message, file, diag.line),
                file=sys.stderr,
            )


def _confirm_apply(workspace: str, count: int, auto_yes: bool) -> bool:
    """True if the apply should proceed: `--yes` was given, or stdin is not
    a terminal (there is no one to ask -- the pipeline that invoked `apply`
    is the actual gate, e.g. code review plus a merge), or it is and the
    user answers yes to the prompt."""
    if auto_yes or not sys.stdin.isatty():
        return True
    answer = input(f"apply {count} capability change(s) to workspace {workspace!r}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "port":
        return _port(parser, args)

    if args.command == "serve":
        from .serve import serve

        relay = args.relay or os.environ.get("RELAY_URL")
        pool_id = args.pool_id or os.environ.get("POOL_ID")
        missing = [
            name
            for name, value in (("--relay/RELAY_URL", relay), ("--pool/POOL_ID", pool_id))
            if not value
        ]
        if missing:
            parser.error(
                "serve needs "
                + " and ".join(missing)
                + ". The runner injects RELAY_URL and POOL_ID as env vars; pass the "
                "flags only when running the image by hand."
            )

        serve(
            relay=relay,
            pool_id=pool_id,
            workdir=Path(args.workdir),
            capability_dir=Path(args.capability_dir) if args.capability_dir else None,
            token_file=Path(args.token_file) if args.token_file else None,
            allow_bundles=bool(args.allow_bundles) or os.environ.get("RW_ALLOW_BUNDLES") == "1",
        )
        return 0

    if args.command == "run":
        if _is_v13_run(parser, args):
            return _run_v13(parser, args)
        if args.local:
            from .run_local import run_local_bundle

            if not args.tasks:
                parser.error("run --local needs at least one --task")
            if args.capability_dir or args.request:
                parser.error("run --local takes no capability_dir/--request; use --local DIR")

            result = run_local_bundle(
                bundle_dir=Path(args.local),
                tasks=args.tasks,
                inputs=json.loads(args.inputs) if args.inputs else None,
                target=json.loads(args.target) if args.target else None,
                credentials_path=Path(args.credentials) if args.credentials else None,
                workdir=Path(args.workdir) if args.workdir else None,
                keep_workdir=args.keep_workdir,
                allow_anonymous=args.allow_anonymous,
            )
            print(json.dumps(result.model_dump(mode="json"), indent=2))
            return 0

        from .run_local import run_local

        if not args.capability_dir or not args.request:
            parser.error("run needs capability_dir and --request (or use --local)")

        result = run_local(
            capability_dir=Path(args.capability_dir),
            request_path=Path(args.request),
            credentials_path=Path(args.credentials) if args.credentials else None,
            workdir=Path(args.workdir) if args.workdir else None,
            keep_workdir=args.keep_workdir,
            allow_anonymous=args.allow_anonymous,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0

    if args.command == "label":
        from .label import ManifestLabelError, encode_manifest, schemas_label

        capability_dir = Path(args.capability_dir)
        try:
            value = (
                schemas_label(capability_dir) if args.schemas else encode_manifest(capability_dir)
            )
        except ManifestLabelError as exc:
            mode = "label --schemas" if args.schemas else "label"
            print(f"rwtask {mode}: {exc}", file=sys.stderr)
            return 1
        sys.stdout.write(value)
        return 0

    if args.command == "schemas":
        from .schemas import SchemaConfigError, check_schemas, write_schemas

        project_dir = Path(args.project_dir)
        try:
            if not args.check:
                for relpath in write_schemas(project_dir):
                    print(f"wrote {relpath}")
                return 0
            checked, problems = check_schemas(project_dir)
        except SchemaConfigError as exc:
            print(f"rwtask schemas: {exc}", file=sys.stderr)
            return 1
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            print(
                "rwtask schemas: the files above do not match their models; run "
                "`rwtask schemas` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"{checked} schema file(s) up to date")
        return 0

    if args.command == "plan":
        from . import gitops

        cfg = _gitops_config(parser, args, "plan")
        try:
            capabilities = gitops.plan(cfg, Path(args.dir), prune=args.prune)
        except gitops.LocalValidationError as exc:
            _print_local_validation_errors(exc.invalid)
            return 1
        except gitops.GitOpsError as exc:
            print(f"rwtask plan: {exc}", file=sys.stderr)
            return 1
        _print_plan(capabilities)
        return 2 if gitops.has_changes(capabilities) else 0

    if args.command == "apply":
        from . import gitops

        cfg = _gitops_config(parser, args, "apply")
        try:
            capabilities = gitops.plan(cfg, Path(args.dir), prune=args.prune)
        except gitops.LocalValidationError as exc:
            _print_local_validation_errors(exc.invalid)
            return 1
        except gitops.GitOpsError as exc:
            print(f"rwtask apply: {exc}", file=sys.stderr)
            return 1

        _print_plan(capabilities)

        conflicts = gitops.conflicted(capabilities)
        if conflicts and not args.adopt:
            print(
                "rwtask apply: "
                + ", ".join(conflicts)
                + " already exist and are not git-managed; rerun with --adopt to take them over",
                file=sys.stderr,
            )
            return 1

        if not gitops.has_changes(capabilities):
            print("nothing to apply")
            return 0

        if not _confirm_apply(cfg.workspace, len(capabilities), args.yes):
            print("apply cancelled")
            return 1

        try:
            applied = gitops.apply(
                cfg, Path(args.dir), message=args.message, prune=args.prune, adopt=args.adopt
            )
        except gitops.LocalValidationError as exc:
            _print_local_validation_errors(exc.invalid)
            return 1
        except gitops.GitOpsError as exc:
            print(f"rwtask apply: {exc}", file=sys.stderr)
            return 1

        _print_plan(applied)
        return 0

    if args.command == "export":
        from . import gitops

        cfg = _gitops_config(parser, args, "export")
        try:
            capabilities = gitops.export(cfg, names=args.names)
        except gitops.GitOpsError as exc:
            print(f"rwtask export: {exc}", file=sys.stderr)
            return 1

        out_dir = Path(args.dir)
        if not args.force:
            conflicts = gitops.export_conflicts(out_dir, capabilities)
            if conflicts:
                for path in conflicts:
                    print(
                        f"rwtask export: {path} has local changes; rerun with --force to overwrite",
                        file=sys.stderr,
                    )
                return 1

        for path in gitops.write_export(out_dir, capabilities):
            print(f"wrote {path}")
        return 0

    if args.command == "test":
        if _is_v13_test(args):
            return _test_v13(parser, args)
        from . import gitops

        results = gitops.run_tests(Path(args.dir), task=args.task)
        passed = failed = no_test = 0
        for result in results:
            label = f"{result.capability}/{result.task}"
            if result.outcome == "pass":
                passed += 1
                print(f"ok    {label}")
            elif result.outcome == "no test":
                no_test += 1
                print(f"--    {label}: no test")
            else:
                failed += 1
                print(f"FAIL  {label}: {result.detail}")
        print(f"{passed} passed, {failed} failed, {no_test} no test")
        return 1 if failed else 0

    if args.command == "upconvert":
        return _upconvert(args)

    if args.command == "validate":
        from .plugin import load_dir, load_skill_dir, validate_tree

        dependencies = _load_dependencies("rwtask validate", args.with_specs)
        if dependencies is None:
            return 2
        directory = Path(args.dir)
        if (directory / "plugin.yaml").is_file():
            tree = load_dir(directory)
        elif (directory / "SKILL.md").is_file():
            tree = load_skill_dir(directory)
        else:
            print(
                f"rwtask validate: {directory} has neither a plugin.yaml nor a SKILL.md",
                file=sys.stderr,
            )
            return 1
        diagnostics = validate_tree(tree, dependencies=dependencies)
        for diag in diagnostics:
            print(_format_plugin_diagnostic(diag))
        errors = sum(d.severity == "error" for d in diagnostics)
        warnings = len(diagnostics) - errors
        print(f"rwtask validate: {errors} error(s), {warnings} warning(s)", file=sys.stderr)
        return 1 if errors else 0

    if args.command == "compile":
        from .contracts import TreeEntry, archive
        from .plugin import compile_tree, load_dir, validate_tree

        dependencies = _load_dependencies("rwtask compile", args.with_specs)
        if dependencies is None:
            return 2
        directory = Path(args.dir)
        if not (directory / "plugin.yaml").is_file():
            print(f"rwtask compile: {directory} has no plugin.yaml", file=sys.stderr)
            return 1
        tree = load_dir(directory)
        diagnostics = validate_tree(tree, dependencies=dependencies)
        for diag in diagnostics:
            print(_format_plugin_diagnostic(diag))
        if any(d.severity == "error" for d in diagnostics):
            return 1
        compiled = compile_tree(tree, dependencies=dependencies)
        try:
            tar = archive.build(
                TreeEntry(path, data, path in tree.executable) for path, data in tree.files.items()
            )
        except archive.ArchiveError as exc:
            print(f"rwtask compile: {exc}", file=sys.stderr)
            return 1
        stem = f"{tree.plugin['name']}-{tree.plugin['version']}"
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{stem}.tar").write_bytes(tar)
        text = json.dumps(compiled, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        (out / f"{stem}.compiled.json").write_bytes(text.encode("utf-8"))
        print(archive.archive_sha256(tar))
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
