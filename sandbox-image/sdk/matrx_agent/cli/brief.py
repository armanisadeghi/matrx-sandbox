"""``mtx brief`` / ``mtx repos tidy`` / ``mtx session start`` — the session brief.

* ``mtx brief``            re-read every repo's state (no network, no changes),
                           rewrite ``~/.matrx/session-brief.{md,json}`` and print it.
  ``mtx brief --cached``   print the last brief without regenerating it.
  ``mtx brief --json``     print the JSON twin instead of Markdown.
* ``mtx repos tidy``       fetch --prune every repo, apply the safe moves
                           (leave a gone+merged branch for the default branch,
                           fast-forward a clean default branch), rewrite and print
                           the brief. ``--dry-run`` fetches and reports only.
* ``mtx session start``    what the boot and binding triggers run: tidy + brief.
                           ``--detach`` returns at once and works in the background.

The logic lives in :mod:`matrx_agent.session_brief`.
"""

from __future__ import annotations

import sys

from matrx_agent import session_brief as sb


def add_subparsers(sub) -> None:
    brief_p = sub.add_parser("brief", help="Print a fresh session brief (repos, image, toolchain, warnings)")
    brief_p.add_argument("--cached", action="store_true", help="print the last brief without regenerating")
    brief_p.add_argument("--json", action="store_true", help="print the JSON twin")

    repos_p = sub.add_parser("repos", help="Repo housekeeping for ~/repos")
    repos_sub = repos_p.add_subparsers(dest="repos_cmd", required=True)
    tidy_p = repos_sub.add_parser("tidy", help="Fetch + safely tidy every repo, then print the brief")
    tidy_p.add_argument("--dry-run", action="store_true", help="fetch and report; change nothing")
    tidy_p.add_argument("--json", action="store_true", help="print the JSON twin")

    sess_p = sub.add_parser("session", help="Session lifecycle hooks")
    sess_sub = sess_p.add_subparsers(dest="session_cmd", required=True)
    start_p = sess_sub.add_parser("start", help="Tidy repos and write the session brief (boot/binding trigger)")
    start_p.add_argument("--detach", action="store_true", help="run in the background and return immediately")
    start_p.add_argument("--trigger", default="session-start")
    start_p.add_argument("--no-tidy", action="store_true", help="fetch + report only")
    start_p.add_argument("--quiet", action="store_true")


def _print_cached(as_json: bool) -> int:
    path = sb.BRIEF_JSON if as_json else sb.BRIEF_MD
    try:
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    except OSError:
        print(f"[mtx brief] no brief at {path} yet — run `mtx brief` or `mtx repos tidy`", file=sys.stderr)
        return 1


def _run_and_print(*, fetch: bool, apply: bool, trigger: str, as_json: bool, quiet: bool = False) -> int:
    code, md = sb.run(fetch=fetch, apply=apply, trigger=trigger)
    if code == 75:
        print("[mtx] another brief/tidy run is in progress; showing the last brief", file=sys.stderr)
        return 0 if quiet else _print_cached(as_json)
    if quiet:
        print(f"[mtx] session brief written to {sb.BRIEF_MD}")
        return code
    if as_json:
        return _print_cached(True)
    sys.stdout.write(md or "")
    return code


def run(args) -> int:
    if args.cmd == "brief":
        if args.cached:
            return _print_cached(args.json)
        return _run_and_print(fetch=False, apply=False, trigger="mtx brief", as_json=args.json)
    if args.cmd == "repos" and args.repos_cmd == "tidy":
        return _run_and_print(
            fetch=True,
            apply=not args.dry_run,
            trigger="mtx repos tidy" + (" --dry-run" if args.dry_run else ""),
            as_json=args.json,
        )
    if args.cmd == "session" and args.session_cmd == "start":
        if args.detach:
            ok = sb.spawn_detached(args.trigger, apply=not args.no_tidy)
            if not args.quiet:
                print(f"[mtx] session brief {'started in background' if ok else 'could not start'}; "
                      f"log {sb.LOG_PATH}")
            return 0 if ok else 1
        return _run_and_print(fetch=True, apply=not args.no_tidy, trigger=args.trigger,
                              as_json=False, quiet=args.quiet)
    return 2
