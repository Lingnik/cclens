"""Command line entry point: index, serve, doctor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, index, query, server


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cclens", description=__doc__)
    parser.add_argument("--version", action="version", version=f"cclens {__version__}")
    parser.add_argument("--claude-home", dest="roots", metavar="PATH",
                        help="roots to index, colon separated, each optionally name=path")
    parser.add_argument("--db", dest="db_path", metavar="PATH", help="index location")
    parser.add_argument("--exclude", metavar="KINDS",
                        help=f"source kinds to skip: {', '.join(config.KINDS)}")
    parser.add_argument("--project-dirs", dest="project_dirs", metavar="NAMES",
                        help="transcript directories to read under each root,"
                             " comma separated (default: projects)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="read new lines into the index")
    p_index.add_argument("--full", action="store_true", help="reread every file from zero")
    p_index.add_argument("--quiet", action="store_true", help="only print the summary")

    p_serve = sub.add_parser("serve", help="serve the browser on localhost")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--index", action="store_true", help="index before serving")
    p_serve.add_argument("--open", dest="open_browser", action="store_true",
                         help="open the page in your browser once the server is up")

    sub.add_parser("doctor", help="show configuration, sources and anything skipped")
    return parser


def cmd_index(cfg: config.Config, args: argparse.Namespace) -> int:
    def progress(item: index.FileReport) -> None:
        if args.quiet:
            return
        note = " (reread)" if item.reindexed else ""
        bad = f"  {item.lines_bad} unparseable" if item.lines_bad else ""
        skip = f"  skipped: {item.skipped_reason}" if item.skipped_reason else ""
        print(f"  {item.source_kind:11} {item.lines_ok:>7} lines{note}{bad}{skip}"
              f"  {Path(item.path).name}")

    print(f"indexing into {cfg.db_path}")
    for root in cfg.roots:
        print(f"  root {root.name}: {root.path}")
    report = index.index_all(cfg, full=args.full, progress=progress)
    print(f"\n{report.lines_ok} entries from {report.files_changed} changed file(s) "
          f"of {report.files_seen} seen, {human_bytes(report.bytes_read)} read "
          f"in {report.elapsed_s:.1f}s")
    if report.lines_bad:
        print(f"{report.lines_bad} unparseable line(s)")
    if report.files_duplicate:
        print(f"{report.files_duplicate} file(s) skipped as the same file reached "
              "through more than one root")
    if report.files_missing:
        print(f"{report.files_missing} previously indexed file(s) are gone from disk, "
              "their entries are kept")
    for item in report.skipped:
        print(f"skipped {item.path}: {item.skipped_reason}")
    print(f"index is {human_bytes(report.db_bytes)}")
    return 0


def cmd_doctor(cfg: config.Config, args: argparse.Namespace) -> int:
    print(f"cclens {__version__}")
    print(f"index      {cfg.db_path}"
          f"{'' if cfg.db_path.exists() else '  (not built yet, run: cclens index)'}")
    print(f"bind       {cfg.host}:{cfg.port}"
          f"{'' if cfg.loopback_only else '  (not loopback, reachable from the network)'}")
    print(f"search cap {cfg.fts_chars} chars per entry")
    if cfg.exclude:
        print(f"excluded   {', '.join(sorted(cfg.exclude))}")
    for root in cfg.roots:
        print(f"root       {root.name}: {root.path}")
    print(f"trees      {', '.join(cfg.project_dirs)}")
    print()
    from .sources import SOURCES
    for root in cfg.roots:
        for source in SOURCES:
            state = "on" if cfg.enabled(source.kind) else "off"
            files = list(source.discover(root)) if cfg.enabled(source.kind) else []
            size = sum(f.path.stat().st_size for f in files if f.path.exists())
            print(f"  {root.name:10} {source.kind:11} {state:3} {len(files):>4} file(s)"
                  f"  {human_bytes(size)}")
    unread = sorted({
        found.name for root in cfg.roots
        for found in root.path.glob("projects*")
        if found.is_dir() and found.name not in cfg.project_dirs
    })
    if unread:
        print(f"\nthere are transcript directories here that are not being read:"
              f" {', '.join(unread)}")
        print("read them with: --project-dirs "
              f"{','.join([*cfg.project_dirs, *unread])}")

    if not cfg.db_path.exists():
        return 0
    db = index.connect(cfg.db_path, read_only=True)
    print()
    for row in query.doctor(db, cfg.enabled_kinds):
        print(f"  {row}")
    db.close()
    return 0


def cmd_serve(cfg: config.Config, args: argparse.Namespace) -> int:
    if args.index:
        cmd_index(cfg, argparse.Namespace(full=False, quiet=True))
    if not cfg.db_path.exists():
        print(f"no index at {cfg.db_path}. Run: cclens index", file=sys.stderr)
        return 1
    return server.serve(cfg, open_browser=args.open_browser)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = config.from_env()
        cfg = config.override(
            cfg,
            roots=args.roots,
            db_path=args.db_path,
            exclude=args.exclude,
            project_dirs=args.project_dirs,
            host=getattr(args, "host", None),
            port=getattr(args, "port", None),
        )
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    return {"index": cmd_index, "serve": cmd_serve, "doctor": cmd_doctor}[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
