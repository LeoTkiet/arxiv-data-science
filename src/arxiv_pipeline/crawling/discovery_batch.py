"""Batch discovery of arXiv IDs in chunks (e.g. 50 IDs per request).

    python -m arxiv_pipeline.crawling.discovery_batch --output data --range 2506.15788:2506.20787
    python -m arxiv_pipeline.crawling.discovery_batch --output data --ids 2506.15788 2506.15789

Results land directly in `state/discovery/<sha256>.json`, so a subsequent `main.py --stage crawl --resume`
reuses them without making individual API calls to arXiv.
"""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import sys
import time

from arxiv_pipeline.common.schemas import DiscoveryRecord, ErrorInfo, VersionRecord
from arxiv_pipeline.common.utils import atomic_json, normalize_id, utc_now, version_paths
from arxiv_pipeline.crawling.discovery import ArxivClient, parse_entry, parse_feed_entries, APIError, DataError
from arxiv_pipeline.pipeline.crawl import _complete_discovery, _read

LOG = logging.getLogger("arxiv_pipeline.discovery_batch")


def is_discovery_done(output: Path, identifier: str) -> bool:
    key = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    checkpoint = output / "state/discovery" / f"{key}.json"
    record = _read(checkpoint)
    return _complete_discovery(record, identifier)


def build_record_from_entry(base: str, entry: dict, client: ArxivClient) -> DiscoveryRecord:
    record = DiscoveryRecord(base, base, None, "success", utc_now())
    count = entry["version"]
    record.metadata = {k: v for k, v in entry.items() if k not in {"version", "updated_date"}}
    record.metadata.update({"latest_version": count, "revised_dates": [], "source": "arxiv_api",
                            "retrieved_at": record.timestamp})
    for v_num in range(1, count + 1):
        if v_num == count:
            v_meta = entry
        else:
            try:
                v_meta = parse_entry(client.fetch(f"{base}v{v_num}"), base, v_num)
            except Exception:
                v_meta = entry
        source, corpus = version_paths(base, v_num)
        record.versions.append(VersionRecord(
            v_num, f"{base}v{v_num}", f"https://arxiv.org/src/{base}v{v_num}",
            source, corpus, v_meta["submission_date"], v_meta["updated_date"],
            v_meta["title"], v_meta["authors"], v_meta["venue"]
        ))
        if v_num == 1:
            record.metadata["submission_date"] = v_meta["submission_date"]
        else:
            record.metadata["revised_dates"].append(v_meta["updated_date"])
    return record


def pre_discover_batch(identifiers, output: Path, client: ArxivClient | None = None, batch_size: int = 50,
                       on_progress=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    client = client or ArxivClient()

    clean_ids = []
    for item in dict.fromkeys(identifiers):
        try:
            base, _ = normalize_id(item)
            if base not in clean_ids:
                clean_ids.append(base)
        except ValueError:
            pass

    todo = [identifier for identifier in clean_ids if not is_discovery_done(output, identifier)]
    total_todo = len(todo)
    if not todo:
        LOG.info("All %d IDs already discovered.", len(clean_ids))
        return {"total": len(clean_ids), "discovered": 0, "already_done": len(clean_ids)}

    LOG.info("Batch discovery starting for %d missing IDs (batch size %d)...", total_todo, batch_size)
    discovered_count = 0
    start_time = time.time()

    for i in range(0, total_todo, batch_size):
        chunk = todo[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total_todo + batch_size - 1) // batch_size
        try:
            payload = client.fetch_batch(chunk)
            entries = parse_feed_entries(payload)
        except Exception as exc:
            LOG.warning("Batch %d/%d failed (%s); falling back to sequential for this batch", batch_num, total_batches, exc)
            entries = {}

        for identifier in chunk:
            key = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
            checkpoint = output / "state/discovery" / f"{key}.json"
            if identifier in entries:
                rec = build_record_from_entry(identifier, entries[identifier], client)
            else:
                try:
                    from arxiv_pipeline.crawling.discovery import discover
                    rec = discover(identifier, client)
                except Exception as exc:
                    rec = DiscoveryRecord(identifier, identifier, None, "not_found", utc_now(),
                                          error=ErrorInfo("not_found", str(exc)))
            atomic_json(checkpoint, rec.to_dict())
            discovered_count += 1

        elapsed = time.time() - start_time
        rate = discovered_count / elapsed if elapsed > 0 else 0
        status_msg = f"[Batch {batch_num}/{total_batches}] Discovered {discovered_count}/{total_todo} IDs ({rate:.1f} IDs/s)"
        LOG.info(status_msg)
        if on_progress:
            on_progress(discovered_count, total_todo, status_msg)

    return {"total": len(clean_ids), "discovered": discovered_count, "already_done": len(clean_ids) - total_todo}


def main(argv=None):
    from arxiv_pipeline.main import expand_range
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", nargs="+", default=[], help="IDs, arXiv URLs or comma-separated IDs")
    parser.add_argument("--input", type=Path, help="UTF-8 text, one ID per line")
    parser.add_argument("--range", dest="id_ranges", action="append", type=expand_range, default=[],
                        metavar="START:END", help="Inclusive range within one month")
    parser.add_argument("--output", type=Path, required=True, help="Output data directory")
    parser.add_argument("--batch-size", type=int, default=50, help="IDs per arXiv query (default: 50)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    identifiers = []
    for item in args.ids:
        identifiers.extend(part.strip() for part in item.split(",") if part.strip())
    if args.input:
        identifiers.extend(line.split("#", 1)[0].strip() for line in args.input.read_text(encoding="utf-8-sig").splitlines()
                           if line.split("#", 1)[0].strip())
    for values in args.id_ranges:
        identifiers.extend(values)

    if not identifiers:
        parser.error("Supply --ids, --input or --range")

    result = pre_discover_batch(identifiers, args.output, batch_size=args.batch_size)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
