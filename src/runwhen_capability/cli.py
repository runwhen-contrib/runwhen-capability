"""`rwtask` -- the task host CLI:

    rwtask serve --relay <url> --pool <poolId> --workdir /work
    rwtask run <capability-dir> --request request.json [--credentials creds.json]
    rwtask label [--schemas] <capability-dir>
    rwtask schemas [--check] [--project-dir DIR]

See serve.py, run_local.py, label.py and schemas.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rwtask")
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

    run_p = sub.add_parser("run", help="run a request against a capability directory, locally")
    run_p.add_argument(
        "capability_dir", help="path to the capability directory (has manifest.yaml, tasks.py)"
    )
    run_p.add_argument(
        "--request", required=True, help="path to a request.json (a RequestEnvelope)"
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

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = _build_parser()
    args = parser.parse_args(argv)

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
        )
        return 0

    if args.command == "run":
        from .run_local import run_local

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

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
