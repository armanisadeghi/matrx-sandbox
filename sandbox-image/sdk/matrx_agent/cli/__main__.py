"""``mtx`` CLI entry point — dispatches to subcommand modules.

Run as ``python3 -m matrx_agent.cli <subcommand> [args...]``. A wrapper at
``/usr/local/bin/mtx`` (installed by the Dockerfile) lets users type
``mtx <subcommand>`` directly.
"""

from __future__ import annotations

import argparse
import os
import sys


def _files_subparser(parent):
    p = parent.add_parser("files", help="Work with AI Dream cloud_files (cld_files)")
    files_sub = p.add_subparsers(dest="files_cmd", required=True)

    files_sub.add_parser("ls", help="List files for this user")
    cat_p = files_sub.add_parser("cat", help="Print one file's content to stdout")
    cat_p.add_argument("path", help="File path within the user's cld_files namespace")

    put_p = files_sub.add_parser("put", help="Upload a local file")
    put_p.add_argument("local_path", help="Local file to upload")
    put_p.add_argument(
        "remote_path",
        nargs="?",
        help="Path within cld_files (defaults to local basename)",
    )

    rm_p = files_sub.add_parser("rm", help="Delete a file from cld_files")
    rm_p.add_argument("path")

    sync_p = files_sub.add_parser("sync", help="Bulk sync — used at sandbox start/stop")
    sync_sub = sync_p.add_subparsers(dest="sync_dir", required=True)

    down_p = sync_sub.add_parser("down", help="Pull all user files into a local dir")
    down_p.add_argument("--dest", default="/home/agent/cloud-files")
    down_p.add_argument("--max-bytes", type=int, default=500_000_000)

    up_p = sync_sub.add_parser("up", help="Push a local dir back to cld_files")
    up_p.add_argument("--src", default="/home/agent/cloud-files")

    # Versioning commands — only on :aidream images (in-sandbox FastAPI on :8001).
    # On :core / :local they print a friendly "spawn an :aidream sandbox" hint
    # and return 1.
    versions_p = files_sub.add_parser(
        "versions",
        help="List version history for a cloud file (:aidream image only)",
    )
    versions_p.add_argument("path", help="File path within cld_files")

    restore_p = files_sub.add_parser(
        "restore",
        help="Restore a previous version of a cloud file (:aidream image only)",
    )
    restore_p.add_argument("path", help="File path within cld_files")
    restore_p.add_argument("version", type=int, help="Version number to restore")

    diff_p = files_sub.add_parser(
        "diff",
        help="Diff two versions of a cloud file (:aidream image only)",
    )
    diff_p.add_argument("path", help="File path within cld_files")
    diff_p.add_argument("v1", type=int, help="Older version number")
    diff_p.add_argument("v2", type=int, help="Newer version number")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mtx", description="Matrx sandbox CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _files_subparser(sub)
    sub.add_parser("whoami", help="Print sandbox identity + AI Dream config")

    # `mtx new python <name>` / `mtx new node <name>` — the canonical project
    # setup recipe. Scaffolds a flat project at ~/projects/<name> with one
    # passing test and prints the next commands.
    new_p = sub.add_parser(
        "new",
        help="Scaffold a runnable project at ~/projects/<name> (python | node)",
    )
    new_p.add_argument("kind", choices=["python", "node"])
    new_p.add_argument("name", help="Project name (lowercase, e.g. 'scraper')")

    # `mtx toolchain ensure` — the self-service upgrade for a box created from
    # an older image. Installs uv/pnpm/gh when missing; `mtx new` calls it first
    # so the sanctioned recipe works on ANY box without a migration.
    tc_p = sub.add_parser(
        "toolchain",
        help="Ensure uv/pnpm/gh are installed on this box (works on old images)",
    )
    tc_sub = tc_p.add_subparsers(dest="toolchain_cmd", required=True)
    for tc_name, tc_help in (
        ("ensure", "Install any missing toolchain binary (idempotent)"),
        ("check", "Report which toolchain binaries are present"),
    ):
        tc_one = tc_sub.add_parser(tc_name, help=tc_help)
        tc_one.add_argument(
            "tools",
            nargs="*",
            help="Limit to these tools (default: uv pnpm gh)",
        )

    # `mtx self-update` — install the SDK the orchestrator staged at
    # /opt/sandbox/sdk.incoming (the agent-side half of the binding-time
    # refresh). Same code path the orchestrator runs; never touches the home.
    su_p = sub.add_parser(
        "self-update",
        help="Install the staged current SDK into this box (never touches your home)",
    )
    su_p.add_argument("--source", default=None, help="staged SDK tree (default /opt/sandbox/sdk.incoming)")
    su_p.add_argument("--target", default=None, help="install location (default /opt/sandbox/sdk)")
    su_p.add_argument("--image-id", default="", help="image identity to stamp")
    su_p.add_argument("--image-version", default="", help="image version to stamp")
    su_p.add_argument(
        "--allow-daemon-restart",
        action="store_true",
        help="restart the in-container daemon when its code changed (drops live terminal sessions)",
    )
    su_p.add_argument("--status", action="store_true", help="print what SDK this box carries and exit")

    # `mtx aidream <subcommand> [args...]` — dispatches to a shell helper.
    # Only available on matrx-sandbox:aidream image variants. We use
    # parser.parse_known_args so the subcommand args pass through untouched.
    aidream_p = sub.add_parser(
        "aidream",
        help="Manage the in-sandbox aidream working copy (image variant only)",
        add_help=False,  # Let aidream-helpers.sh print its own help.
    )
    aidream_p.add_argument("aidream_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    # Lazy import — keeps `mtx --help` fast even if the cloud_files module
    # has heavy imports.
    if args.cmd == "whoami":
        from matrx_agent.cli.whoami import run as whoami_run
        return whoami_run()

    if args.cmd == "new":
        from matrx_agent.cli.new import run as new_run
        return new_run(args)

    if args.cmd == "toolchain":
        from matrx_agent.cli.toolchain import run as toolchain_run
        return toolchain_run(args)

    if args.cmd == "self-update":
        from matrx_agent import selfupdate

        sub_argv = []
        if args.source:
            sub_argv += ["--source", args.source]
        if args.target:
            sub_argv += ["--target", args.target]
        if args.image_id:
            sub_argv += ["--image-id", args.image_id]
        if args.image_version:
            sub_argv += ["--image-version", args.image_version]
        if args.allow_daemon_restart:
            sub_argv.append("--allow-daemon-restart")
        if args.status:
            sub_argv.append("--status")
        return selfupdate.main(sub_argv)

    if args.cmd == "files":
        from matrx_agent.cli.files import run as files_run
        return files_run(args)

    if args.cmd == "aidream":
        helper = "/opt/sandbox/scripts/aidream-helpers.sh"
        if not os.path.exists(helper):
            print(
                "[mtx aidream] this sandbox image doesn't include aidream — "
                "spawn one with template='aidream' to use these commands.",
                file=sys.stderr,
            )
            return 1
        os.execv(helper, [helper, *args.aidream_args])

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
