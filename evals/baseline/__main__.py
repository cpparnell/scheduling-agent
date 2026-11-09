"""python -m evals.baseline {status,run,episodes,label,score}

  status    what's done and what's next
  run       dry-run v0.14 over the last N days (costs API $; preflight-capped),
            then build the labeling queue
  episodes  rebuild the labeling queue from an existing run (free)
  label     open the local labeling UI (free)
  score     per-failure-mode report from the labels so far (free)
"""

import argparse
import logging
from datetime import date

from evals.baseline import episodes, labels, paths, run_v014


def cmd_episodes() -> None:
    meta = run_v014.load_meta()
    if meta is None:
        raise SystemExit("No v0.14 run yet. Run `python -m evals.baseline run` first.")
    if not meta.get("valid"):
        raise SystemExit("The v0.14 run had failed API calls, so its detections are incomplete. Re-run it.")
    from scheduling_agent import config

    since = date.fromisoformat(str(meta["since"]))
    until = date.fromisoformat(str(meta["until"]))
    stats = episodes.scan_days(since, until, config.load()["blocked_contacts"])
    queue = episodes.build(stats, episodes.load_detections(paths.DETECTIONS))
    episodes.write(queue, paths.EPISODES)
    print(f"Labeling queue: {len(queue)} episodes {episodes.stratum_sizes(queue)} → {paths.EPISODES}")


def cmd_status() -> None:
    meta = run_v014.load_meta()
    print(f"Corpus dir: {paths.CORPUS_DIR}")
    if meta is None:
        print("1. v0.14 run: NOT DONE  →  python -m evals.baseline run")
        return
    print(f"1. v0.14 run: {meta['since']}..{meta['until']}, valid={meta['valid']}, "
          f"cost ${meta['cost_usd']}, {meta['n_events']} detections")
    if not paths.EPISODES.exists():
        print("2. Queue: NOT BUILT  →  python -m evals.baseline episodes")
        return
    q = episodes.read(paths.EPISODES)
    done = labels.load(paths.LABELS)
    n = sum(1 for e in q if e.id in done)
    print(f"2. Queue: {len(q)} episodes {episodes.stratum_sizes(q)}")
    print(f"3. Labeled: {n} (aim ~300)  →  python -m evals.baseline label")
    print("4. Report  →  python -m evals.baseline score")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m evals.baseline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--cap", type=float, default=15.0, help="abort if projected cost exceeds this (USD)")
    r.add_argument("--until", type=date.fromisoformat, default=None,
                   help="last day (exclusive) of the range, YYYY-MM-DD; default today")
    sub.add_parser("episodes")
    lab = sub.add_parser("label")
    lab.add_argument("--no-browser", action="store_true")
    sub.add_parser("score")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    paths.ensure()

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "run":
        out = run_v014.run(args.days, args.cap, args.until)
        print(f"v0.14 run done: {out.n_events} detections, valid={out.valid}, ${out.cost_usd:.2f}")
        if out.valid:
            cmd_episodes()
    elif args.cmd == "episodes":
        cmd_episodes()
    elif args.cmd == "label":
        from evals.baseline import label_server

        label_server.serve(open_browser=not args.no_browser)
    elif args.cmd == "score":
        from evals.baseline import score

        score.main()


if __name__ == "__main__":
    main()
