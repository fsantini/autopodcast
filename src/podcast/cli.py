from __future__ import annotations

import argparse
import logging
import sys
import uuid

from .config import load_config
from pathlib import Path

from .pipeline import publish_dir, run_topic
from .publish import PublishError
from .state import State


def _print_table(papers, chosen_keys):
    for p in sorted(papers, key=lambda p: p.screening.score if p.screening else -1, reverse=True):
        s = p.screening
        mark = "*" if p.key in chosen_keys else " "
        sc = s.summary() if s else "unscreened".ljust(33)
        print(f"{mark} {sc} {p.license or '?':<10} {p.title[:70]}")


def _publish_one(cfg, state, directory: Path) -> bool:
    log = logging.getLogger("podcast")
    try:
        result = publish_dir(cfg, state, directory)
    except (PublishError, OSError, RuntimeError) as e:
        log.error("publishing %s failed: %s", directory, e)
        return False
    print(f"  PUBLISHED ({result.get('status')}): {result.get('page_url')}")
    return True


def _publish_command(cfg, state, args) -> int:
    if args.dirs:
        targets = [d.resolve() for d in args.dirs]
    else:
        targets = [d for _run, topic, d in state.pending_episodes() if args.topic in (None, topic)]
        if not targets:
            print("Nothing to publish: no built episodes are waiting.")
            return 0
    ok = [_publish_one(cfg, state, d) for d in targets]
    return 0 if all(ok) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="podcast")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("screen", help="discover + screen papers (stages 1-2), no side effects beyond state db")
    s.add_argument("--topic", default=None, help="topic name (default: all)")
    s.add_argument("--no-state", action="store_true", help="do not record results (dry run)")
    f = sub.add_parser("fetch", help="stages 1-3: discover, screen, download PDFs of the top papers")
    f.add_argument("--topic", default=None)
    f.add_argument("--no-state", action="store_true")
    r = sub.add_parser("run", help="full pipeline (stages 1-4): discover, screen, download, build the episode")
    r.add_argument("--topic", default=None)
    r.add_argument("--until", choices=["screen", "fetch", "build"], default="build")
    r.add_argument("--no-state", action="store_true")
    r.add_argument("--publish", action="store_true", help="upload each built episode to the site right away")
    pb = sub.add_parser("publish", help="upload built episodes to the site (default: all that are not published yet)")
    pb.add_argument("dirs", nargs="*", type=Path, help="episode folders (containing episode.json); default: pending ones")
    pb.add_argument("--topic", default=None, help="only pending episodes of this topic")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    topics = [t for t in cfg.topics if args.topic in (None, t.name)]
    if not topics:
        sys.exit(f"no such topic: {args.topic}")
    state = State(cfg.data_dir / "state.db" if not getattr(args, "no_state", False) else ":memory:")
    if args.cmd == "publish":
        return _publish_command(cfg, state, args)
    until = getattr(args, "until", None) or {"screen": "screen", "fetch": "fetch"}[args.cmd]
    failures = 0
    built: list[Path] = []
    for t in topics:
        print(f"\n=== {t.name} ===")
        try:
            screened, chosen, episode = run_topic(cfg, t, state, uuid.uuid4().hex[:8], until)
        except Exception as e:  # one failing topic must not stop the others
            logging.getLogger("podcast").exception("topic %r failed: %s", t.name, e)
            failures += 1
            continue
        _print_table(screened, {p.key for p in chosen})
        if chosen:
            print(f"\n  Selected for the podcast ({len(chosen)}):")
            for i, p in enumerate(chosen, 1):
                print(f"  {i}. {p.title}\n     {p.screening.summary().strip()} (score {p.screening.score:.2f})\n     PDF: {p.pdf_path}")
        if episode:
            print(f"\n  EPISODE: {episode.title}\n  audio:   {episode.audio_path} ({episode.duration_seconds}s)\n  {episode.summary}")
            built.append(Path(episode.audio_path).parent.resolve())
    if getattr(args, "publish", False):
        # this run's episodes plus any left over from earlier failed uploads (a failed upload stays 'built')
        pending = [d for _run, topic, d in state.pending_episodes() if args.topic in (None, topic)]
        targets = list(dict.fromkeys(built + pending))
        if targets:
            print("\n=== publishing ===")
        failures += sum(not _publish_one(cfg, state, d) for d in targets)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
