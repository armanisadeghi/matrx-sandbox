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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mtx", description="Matrx sandbox CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _files_subparser(sub)
    sub.add_parser("whoami", help="Print sandbox identity + AI Dream config")

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
