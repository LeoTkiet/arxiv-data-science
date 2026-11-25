"""Select and stage the T3-05 silver pool without changing source snapshots."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.contracts import ContractError, canonical_id
from arxiv_pipeline.matching.io import load_with_provenance


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _owner(paper: str, ranges: dict) -> str:
    for member, (start, end) in ranges.items():
        if start <= paper <= end:
            return member
    raise ContractError(f"Publication is outside assigned ranges: {paper}")


def select_pool(population_path: Path, ranges_path: Path, output: Path, *, seed: int = 42,
                reserve_per_member: int = 30, target_count: int | None = None) -> dict:
    """Freeze a balanced, reproducible primary selection and reserve queue."""
    population = _read(population_path)
    ids = population.get("publication_ids", population.get("population_ids"))
    manual = population.get("excluded_manual_ids")
    if not isinstance(ids, list) or not isinstance(manual, list):
        raise ContractError("Population manifest needs publication_ids and excluded_manual_ids")
    ids = [canonical_id(p) for p in ids]
    manual = [canonical_id(p) for p in manual]
    if len(ids) != len(set(ids)) or len(manual) != len(set(manual)) or not set(manual) <= set(ids):
        raise ContractError("Population/manual IDs are duplicated or inconsistent")
    raw_ranges = _read(ranges_path)["ranges"]
    if len(raw_ranges) != 5:
        raise ContractError("Expected five member ranges")
    ranges = {f"tv{i+1}": tuple(row["range"].split(":")) for i, row in enumerate(raw_ranges)}
    buckets = {member: [] for member in ranges}
    for paper in ids:
        if paper not in manual:
            buckets[_owner(paper, ranges)].append(paper)
    minimum = max(math.ceil(sum(map(len, buckets.values())) * 0.1), 10)
    target = minimum if target_count is None else target_count
    if type(target) is not int or target < minimum or target > sum(map(len, buckets.values())):
        raise ContractError(f"target_count must be between {minimum} and the non-manual population size")
    if type(reserve_per_member) is not int or reserve_per_member < 0:
        raise ContractError("reserve_per_member must be a nonnegative integer")
    base, extra = divmod(target, len(buckets))
    selected, reserves = {}, {}
    for i, (member, values) in enumerate(buckets.items()):
        ordered = sorted(values, key=lambda p: (hashlib.sha256(f"{seed}:{p}".encode()).hexdigest(), p))
        take = base + (i < extra)
        if len(ordered) < take + reserve_per_member:
            raise ContractError(f"Not enough candidates/reserves for {member}")
        selected[member] = ordered[:take]
        reserves[member] = ordered[take:take + reserve_per_member]
    result = dict(schema_version="1.0", task="T3-05", seed=seed,
                  selection_method="sha256(seed:arxiv_id), balanced across five assigned ranges",
                  population_sha256=_digest(population_path), population_count=len(ids),
                  manual_count=len(manual), non_manual_count=sum(map(len, buckets.values())),
                  minimum_auto_count=minimum, target_auto_count=target,
                  population_snapshot_id=population.get("population_snapshot_id"),
                  selected=selected, reserves=reserves,
                  range_source=str(ranges_path.name))
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, result)
    return result


def prepare_pool(selection_path: Path, roots_path: Path, output: Path) -> dict:
    """Stage verified ready bundles, one paper at a time; reruns skip matching hashes."""
    selection, roots = _read(selection_path), _read(roots_path)
    if selection.get("task") != "T3-05" or not isinstance(roots, dict):
        raise ContractError("Invalid T3-05 selection or roots")
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for member, papers in selection["selected"].items():
        context = roots.get(member)
        if not isinstance(context, dict) or not {"ms1_root", "ms2_root"} <= context.keys():
            raise ContractError(f"Missing MS1/MS2 roots for {member}")
        for paper in papers:
            target = output / "bundles" / member / f"{paper}.json"
            receipt = output / "receipts" / member / f"{paper}.json"
            if target.is_file() and receipt.is_file():
                prior = _read(receipt)
                if prior.get("sha256") == _digest(target) and prior.get("arxiv_id") == paper:
                    records.append(prior)
                    continue
            loaded = load_with_provenance(paper, context)
            if loaded.bundle.readiness != "ready":
                records.append(dict(member=member, arxiv_id=paper, status="not_ready",
                                    issues=[i.to_dict() for i in loaded.bundle.issues]))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(target, loaded.bundle.to_dict())
            row = dict(member=member, arxiv_id=paper, status="ready", bundle=str(target.relative_to(output)),
                       sha256=_digest(target), bibtex_count=len(loaded.bundle.bibtex),
                       reference_count=len(loaded.bundle.references), provenance=loaded.provenance)
            atomic_json(receipt, row)
            records.append(row)
    summary = dict(task="T3-05", selection_sha256=_digest(selection_path),
                   total=len(records), ready=sum(r.get("status") == "ready" for r in records),
                   not_ready=sum(r.get("status") != "ready" for r in records), publications=records)
    atomic_json(output / "prepared_manifest.json", summary)
    return summary
