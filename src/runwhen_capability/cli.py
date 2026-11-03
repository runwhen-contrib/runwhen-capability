"""`rwtask` -- the task host CLI:

    rwtask serve --relay <url> --pool <poolId> --workdir /work
    rwtask run <capability-dir> --request request.json [--credentials creds.json]
    rwtask label [--schemas] <capability-dir>
    rwtask schemas [--check] [--project-dir DIR]
    rwtask plan <dir> [--prune]
    rwtask apply <dir> -m <message> [--prune] [--adopt] [--yes]
    rwtask export <dir> [--name NAME ...] [--force]
    rwtask test <dir> [--task NAME]
    rwtask port <bundle-dir> --plugin NAME --into <skills-dir> [--inventory FILE]
    rwtask validate <dir>
    rwtask compile <plugin-dir> [--out DIR]
    rwtask upconvert <src-dir> --into <skills-dir> [--name NAME] [--force]

See serve.py, run_local.py, label.py, schemas.py, gitops.py, port/ and upconvert/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
        "request -- instead of refusing them; default: off, unless RW_ALLOW_BUNDLES=1. "
        "Only an image built to run custom capabilities should enable this.",
    )

    run_p = sub.add_parser(
        "run",
        help="run a request against a capability directory, or --local a bundle "
        "directory's task(s), locally",
    )
    run_p.add_argument(
        "capability_dir",
        nargs="?",
        default=None,
        help="path to the capability directory (has manifest.yaml, tasks.py) -- omit with --local",
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
        help="run each capability's tests/<task>.json locally, the same way run --local does",
    )
    test_p.add_argument("dir", help="root directory to search for capability.yaml bundles")
    test_p.add_argument(
        "--task",
        default=None,
        metavar="NAME",
        help="run only the test for this task name, across every capability that has one",
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

    compile_p = sub.add_parser(
        "compile",
        help="validate a V13 plugin directory, then write <out>/<name>-<version>.tar and "
        ".compiled.json and print the archive's sha256",
    )
    compile_p.add_argument("dir", help="plugin directory (has plugin.yaml)")
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


def _format_plugin_diagnostic(diag) -> str:
    where = diag.file or ""
    if diag.path:
        where = f"{where}:{diag.path}"
    prefix = "warning: " if diag.severity == "warning" else ""
    head = f"{where}: " if where else ""
    return f"{prefix}{head}{diag.code} {diag.message}"


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
        diagnostics = validate_tree(tree)
        for diag in diagnostics:
            print(_format_plugin_diagnostic(diag))
        errors = sum(d.severity == "error" for d in diagnostics)
        warnings = len(diagnostics) - errors
        print(f"rwtask validate: {errors} error(s), {warnings} warning(s)", file=sys.stderr)
        return 1 if errors else 0

    if args.command == "compile":
        from .contracts import TreeEntry, archive
        from .plugin import compile_tree, load_dir, validate_tree

        directory = Path(args.dir)
        if not (directory / "plugin.yaml").is_file():
            print(f"rwtask compile: {directory} has no plugin.yaml", file=sys.stderr)
            return 1
        tree = load_dir(directory)
        diagnostics = validate_tree(tree)
        for diag in diagnostics:
            print(_format_plugin_diagnostic(diag))
        if any(d.severity == "error" for d in diagnostics):
            return 1
        compiled = compile_tree(tree)
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
