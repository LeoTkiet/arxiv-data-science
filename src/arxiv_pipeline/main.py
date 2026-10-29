"""Command line interface for TV1 discovery and integrated MS1 crawling."""

import argparse
import json
from pathlib import Path

from arxiv_pipeline.common.config import APIConfig
from arxiv_pipeline.common.logger import configure_logging, close_logging
from arxiv_pipeline.common.utils import normalize_id
from arxiv_pipeline.crawling.discovery import ArxivClient
from arxiv_pipeline.pipeline.crawl import run


def expand_range(value):
    try:
        start, end = value.split(":")
        a, av = normalize_id(start)
        b, bv = normalize_id(end)
        if av or bv or "/" in a or "/" in b:
            raise ValueError("Ranges require unversioned modern IDs")
        month, first = a.split(".")
        other_month, last = b.split(".")
        low, high = int(first), int(last)
        if month != other_month or high < low or high - low >= 10000:
            raise ValueError("Range must be increasing, within one month and at most 10000 IDs")
        return [f"{month}.{n:0{len(first)}d}" for n in range(low, high + 1)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid range: {exc}") from exc


def main(argv=None, *, client=None):
    parser = argparse.ArgumentParser(description="Discover every arXiv version and coordinate MS1 modules")
    parser.add_argument("--ids", nargs="+", default=[], help="IDs, arXiv URLs or comma-separated IDs")
    parser.add_argument("--input", type=Path, help="UTF-8 text, one ID per line; # comments accepted")
    parser.add_argument("--range", dest="id_ranges", action="append", type=expand_range, default=[],
                        metavar="START:END", help="Inclusive range within one month; may repeat")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["discovery", "crawl"], default="discovery")
    parser.add_argument("--timings", type=Path, help="Optional JSONL timing events for TV5 (overwritten per run)")
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    restart = parser.add_mutually_exclusive_group()
    restart.add_argument("--resume", action="store_true", help="Reuse complete checkpoints; retry failed records")
    restart.add_argument("--force", action="store_true", help="Refresh discovery and rerun all adapters")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3, help="Extra attempts after initial request")
    parser.add_argument("--request-delay", type=float, default=3, help="Seconds, minimum 3")
    args = parser.parse_args(argv)
    identifiers = []
    for item in args.ids:
        identifiers.extend(part.strip() for part in item.split(",") if part.strip())
    if args.input:
        try:
            identifiers.extend(line.split("#", 1)[0].strip() for line in args.input.read_text(encoding="utf-8-sig").splitlines()
                               if line.split("#", 1)[0].strip())
        except OSError as exc:
            parser.error(str(exc))
    for values in args.id_ranges:
        identifiers.extend(values)
    if not identifiers:
        parser.error("Supply --ids, --input or --range")
    try:
        config = APIConfig(timeout=args.timeout, retries=args.retries, delay=args.request_delay)
    except ValueError as exc:
        parser.error(str(exc))
    logger = configure_logging(args.output)
    timing_stream = None
    try:
        if args.timings:
            args.timings.parent.mkdir(parents=True, exist_ok=True)
            timing_stream = args.timings.open('w', encoding='utf-8')

        def observe(event):
            timing_stream.write(json.dumps(event, ensure_ascii=False) + '\n')
            timing_stream.flush()

        summary = run(identifiers, args.output, client=client or ArxivClient(config),
                      resume=args.resume, full=args.stage == "crawl", observer=observe if timing_stream else None,
                      run_id=args.run_id)
    except (OSError, KeyboardInterrupt) as exc:
        logger.error("Run interrupted; checkpoints can be resumed: %s", exc)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if timing_stream:
            timing_stream.close()
        close_logging(logger)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
