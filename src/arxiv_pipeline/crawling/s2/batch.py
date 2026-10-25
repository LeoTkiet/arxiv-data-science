"""Pre-fill the `references` stage in batches of 100 papers (resumable).

    python -m arxiv_pipeline.crawling.s2.batch --input ids.txt --output data
    python -m arxiv_pipeline.crawling.s2.batch --ids 2301.00001 2301.00002 --output data

Results land in the pipeline layout, so a later `main.py --stage crawl --resume` reuses them
without calling Semantic Scholar again. Papers that ended in error are retried here in extra
passes and, if still failing, on the next run.
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

from arxiv_pipeline.common.schemas import SCHEMA_VERSION
from arxiv_pipeline.common.utils import normalize_id

from . import config
from .s2_source import S2Source
from .store import STAGE, TERMINAL, checkpoint_path, store_outcome

log = logging.getLogger("arxiv_pipeline.s2")


def is_done(output: Path, arxiv_id: str) -> bool:
    """True when a valid terminal checkpoint exists (mirrors the orchestrator's reuse rule)."""
    try:
        cached = json.loads(checkpoint_path(output, arxiv_id).read_text(encoding="utf-8"))
        root = output.resolve()
        files_ok = all((output / rel).resolve().is_relative_to(root) and (output / rel).exists()
                       for rel in cached["artifacts"].values())
        return (cached["schema_version"] == SCHEMA_VERSION and cached["stage"] == STAGE
                and cached["paper_id"] == arxiv_id and cached["version"] is None
                and cached["status"] in TERMINAL and files_ok
                and (cached["status"] != "success" or bool(cached["artifacts"])))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def crawl(ids, output: Path, source: S2Source, on_result=None) -> dict:
    counts = {"ok": 0, "ok_empty": 0, "not_found": 0, "error": 0}
    skipped = 0
    for n_pass in range(1 + config.RETRY_PASSES):
        todo = [i for i in ids if not is_done(output, i)]
        if n_pass == 0:
            skipped = len(ids) - len(todo)
        elif not todo:
            break
        else:
            log.warning("pass %d: retrying %d papers after %ds cool-down", n_pass + 1, len(todo), config.RETRY_COOLDOWN)
            time.sleep(config.RETRY_COOLDOWN)
        counts["error"] = 0
        for outcome in source.fetch(todo):
            store_outcome(outcome, output)
            counts[outcome.status] += 1
            if on_result:
                on_result(outcome)
        if not counts["error"]:
            break
    return {"requested": len(ids), "skipped": skipped, **counts}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", help="text file, one arXiv ID per line (# comments allowed)")
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--output", required=True)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    raw = list(args.ids)
    if args.input:
        raw += [l.split("#")[0].strip() for l in Path(args.input).read_text(encoding="utf-8").splitlines()]
    ids = []
    for text in filter(None, raw):
        try:
            base, _ = normalize_id(text)
        except ValueError as exc:
            log.error("skip %r: %s", text, exc)
            continue
        if base not in ids:
            ids.append(base)
    output = Path(args.output)
    summary = crawl(ids, output, S2Source(output / "state" / "s2_last_call"))
    print(json.dumps(summary, indent=1))
    return 1 if summary["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
