"""Publication-grouped dataset splitting & leakage guards (Milestone 3 · TV5).

Implements publication-level train/valid/test splitting per the official
Milestone 3 specification and docs/MS3_CONTRACTS.md:
- Group strictly by canonical publication ID (never split a publication across partitions).
- For a group of k=5 members (group_size=5):
    valid = 5 manual + 5 auto publications
    test  = 5 manual + 5 auto publications
    train = all remaining selected publications
- Enforces disjoint manual/auto pools and verifies label_source provenance.
- Applies deterministic proportional stratification with gold-coverage balancing
  whenever label statistics make it feasible; never invents labels or shrinks
  official quotas silently.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

from arxiv_pipeline.common.schemas import MATCHING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.contracts import (
    ContractError,
    FeatureBatch,
    LabelRecord,
    PARTITIONS,
    PartitionManifest,
    PartitionRow,
    PublicationBundle,
    RankedPrediction,
    ValidationReport,
    canonical_id,
    validate_split_quotas,
)


def _content_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_label_record(item: LabelRecord | Mapping[str, Any]) -> LabelRecord:
    if isinstance(item, LabelRecord):
        return item
    if isinstance(item, Mapping):
        return LabelRecord(**dict(item))
    raise ContractError(f"Unsupported label entry type: {type(item).__name__}")


def _extract_label_pools(
    label_manifest: Sequence[LabelRecord | Mapping[str, Any]] | Mapping[str, Any],
    *,
    label_snapshot_id: str | None = None,
) -> tuple[list[str], list[str], list[LabelRecord], dict[str, dict[str, Any]], str]:
    """Normalize a label manifest into (manual_ids, auto_ids, labels, pub_stats, snapshot_id)."""
    declared_manual: list[str] | None = None
    declared_auto: list[str] | None = None
    explicit_stats: dict[str, dict[str, Any]] = {}
    raw_labels: Sequence[Any] = ()
    snap_id = label_snapshot_id

    if isinstance(label_manifest, Mapping):
        if snap_id is None and label_manifest.get("label_snapshot_id"):
            snap_id = str(label_manifest["label_snapshot_id"])
        if "manual_publication_ids" in label_manifest or "auto_publication_ids" in label_manifest:
            raw_m = label_manifest.get("manual_publication_ids")
            raw_a = label_manifest.get("auto_publication_ids")
            if raw_m is None or raw_a is None:
                raise ContractError(
                    "label_manifest mapping must supply both manual_publication_ids and auto_publication_ids"
                )
            declared_manual = [canonical_id(p) for p in raw_m]
            declared_auto = [canonical_id(p) for p in raw_a]
            if len(declared_manual) != len(set(declared_manual)):
                raise ContractError("Duplicate publication ID in manual_publication_ids")
            if len(declared_auto) != len(set(declared_auto)):
                raise ContractError("Duplicate publication ID in auto_publication_ids")
        if "labels" in label_manifest and label_manifest["labels"] is not None:
            raw_labels = label_manifest["labels"]
        if isinstance(label_manifest.get("publication_stats"), Mapping):
            for pid, stats in label_manifest["publication_stats"].items():
                explicit_stats[canonical_id(pid)] = dict(stats)
        if declared_manual is None and not raw_labels:
            raise ContractError("label_manifest mapping contains neither labels nor publication pools")
    elif isinstance(label_manifest, Sequence) and not isinstance(label_manifest, (str, bytes)):
        raw_labels = label_manifest
    else:
        raise ContractError("label_manifest must be a sequence of LabelRecord or a manifest mapping")

    labels = [_as_label_record(item) for item in raw_labels]

    seen_queries: set[tuple[str, str]] = set()
    sources_by_pub: dict[str, set[str]] = defaultdict(set)
    computed_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "supervised": 0, "matched": 0, "unmatched": 0, "uncertain": 0}
    )

    for lbl in labels:
        if lbl.key in seen_queries:
            raise ContractError(f"Duplicate label for query {lbl.key}")
        seen_queries.add(lbl.key)
        sources_by_pub[lbl.arxiv_id].add(lbl.label_source)
        st = computed_stats[lbl.arxiv_id]
        st["total"] += 1
        if lbl.decision == "uncertain":
            st["uncertain"] += 1
        if lbl.is_supervised:
            st["supervised"] += 1
            if lbl.decision == "matched":
                st["matched"] += 1
            elif lbl.decision == "unmatched":
                st["unmatched"] += 1

    mixed_pubs = sorted(pid for pid, srcs in sources_by_pub.items() if len(srcs) > 1)
    if mixed_pubs:
        raise ContractError(
            f"Publications mix manual and heuristic labels (pool overlap): {mixed_pubs}"
        )

    inferred_manual = sorted(pid for pid, srcs in sources_by_pub.items() if srcs == {"manual"})
    inferred_auto = sorted(pid for pid, srcs in sources_by_pub.items() if srcs == {"heuristic"})

    if declared_manual is not None and declared_auto is not None:
        manual_set, auto_set = set(declared_manual), set(declared_auto)
        if manual_set & auto_set:
            raise ContractError(
                f"Manual/auto publication pools overlap: {sorted(manual_set & auto_set)}"
            )
        if labels:
            for pid in inferred_manual:
                if pid not in manual_set:
                    raise ContractError(
                        f"Manual-labeled publication {pid} is missing from manual_publication_ids"
                    )
            for pid in inferred_auto:
                if pid not in auto_set:
                    raise ContractError(
                        f"Heuristic-labeled publication {pid} is missing from auto_publication_ids"
                    )
        manual_ids = sorted(manual_set)
        auto_ids = sorted(auto_set)
    else:
        manual_ids = inferred_manual
        auto_ids = inferred_auto

    if not manual_ids and not auto_ids:
        raise ContractError("Cannot create split from empty label pools")

    merged_stats: dict[str, dict[str, Any]] = {}
    for pid in manual_ids + auto_ids:
        if pid in computed_stats:
            merged_stats[pid] = dict(computed_stats[pid])
        if pid in explicit_stats:
            merged_stats.setdefault(pid, {}).update(explicit_stats[pid])

    if not snap_id:
        digest_payload = {
            "manual_publication_ids": manual_ids,
            "auto_publication_ids": auto_ids,
            "labels": [
                lbl.to_dict()
                for lbl in sorted(labels, key=lambda r: (r.arxiv_id, r.citation_key))
            ],
        }
        snap_id = f"labels-{_content_digest(digest_payload)[:16]}"

    return manual_ids, auto_ids, labels, merged_stats, snap_id


def _extract_population_info(
    population_manifest: Any,
    selected_ids: set[str],
    *,
    population_snapshot_id: str | None = None,
) -> tuple[int, str]:
    """Resolve (population_count, population_snapshot_id) and validate pool membership."""
    snap_id = population_snapshot_id
    if population_manifest is None:
        count = len(selected_ids)
        if not snap_id:
            snap_id = f"pop-{_content_digest(sorted(selected_ids))[:16]}"
        return count, snap_id

    if isinstance(population_manifest, Mapping):
        if snap_id is None and population_manifest.get("population_snapshot_id"):
            snap_id = str(population_manifest["population_snapshot_id"])
        if "publication_ids" in population_manifest and population_manifest["publication_ids"] is not None:
            canon_ids = [canonical_id(p) for p in population_manifest["publication_ids"]]
            if len(canon_ids) != len(set(canon_ids)):
                raise ContractError("Duplicate publication ID in population_manifest.publication_ids")
            pop_set = set(canon_ids)
            outside = sorted(selected_ids - pop_set)
            if outside:
                raise ContractError(
                    f"Selected publications outside assigned population scope: {outside}"
                )
            count = len(pop_set)
            if "population_count" in population_manifest and population_manifest["population_count"] is not None:
                declared = population_manifest["population_count"]
                if type(declared) is not int or declared != count:
                    raise ContractError("population_count disagrees with publication_ids length")
            if not snap_id:
                snap_id = f"pop-{_content_digest(sorted(pop_set))[:16]}"
            return count, snap_id
        if "population_count" in population_manifest:
            count = population_manifest["population_count"]
            if type(count) is not int or count < len(selected_ids):
                raise ContractError(
                    "population_manifest.population_count must be an int >= selected publications"
                )
            if not snap_id:
                snap_id = f"pop-n{count}-{_content_digest(sorted(selected_ids))[:12]}"
            return count, snap_id
        raise ContractError(
            "population_manifest mapping must contain 'publication_ids' or 'population_count'"
        )

    if isinstance(population_manifest, (list, tuple, set, frozenset)):
        canon_ids = [canonical_id(p) for p in population_manifest]
        if len(canon_ids) != len(set(canon_ids)):
            raise ContractError("Duplicate publication ID in population_manifest")
        pop_set = set(canon_ids)
        outside = sorted(selected_ids - pop_set)
        if outside:
            raise ContractError(
                f"Selected publications outside assigned population scope: {outside}"
            )
        if not snap_id:
            snap_id = f"pop-{_content_digest(sorted(pop_set))[:16]}"
        return len(pop_set), snap_id

    raise ContractError(f"Unsupported population_manifest type: {type(population_manifest).__name__}")


def _stratum_key(pub_id: str, stats: Mapping[str, Mapping[str, Any]], median_sup: float) -> tuple[int, int]:
    """Coarse deterministic stratum (has_gold, size_bucket) for stratification."""
    info = stats.get(pub_id)
    if not info:
        return (0, 0)
    matched = int(info.get("matched", 0))
    supervised = int(info.get("supervised", info.get("total", 0)))
    return (1 if matched > 0 else 0, 1 if supervised >= median_sup else 0)


def _assign_pool_partitions(
    pub_ids: list[str],
    *,
    valid_count: int,
    test_count: int,
    rng: random.Random,
    stats: Mapping[str, Mapping[str, Any]],
    stratify: bool,
) -> dict[str, str]:
    """Assign each publication in one pool to train/valid/test.

    Uses proportional largest-remainder stratification when per-publication label
    stats reveal multiple strata so valid, test, and train each receive balanced
    gold coverage whenever feasible. Falls back cleanly to a seeded shuffle when
    stats are absent or uniform.
    """
    ordered = sorted(pub_ids)
    needed_eval = valid_count + test_count
    if len(ordered) < needed_eval:
        raise ContractError(
            f"Pool has only {len(ordered)} publications; need at least {needed_eval} for valid + test"
        )

    use_stratified = False
    strata: dict[tuple[int, int], list[str]] = defaultdict(list)
    if stratify and stats:
        sup_counts = sorted(
            int(stats[p].get("supervised", stats[p].get("total", 0)))
            for p in ordered
            if p in stats
        )
        if sup_counts:
            median_sup = float(sup_counts[len(sup_counts) // 2])
            for pid in ordered:
                strata[_stratum_key(pid, stats, median_sup)].append(pid)
            use_stratified = len(strata) > 1

    if not use_stratified:
        shuffled = list(ordered)
        rng.shuffle(shuffled)
        valid_ids = shuffled[:valid_count]
        test_ids = shuffled[valid_count : valid_count + test_count]
        train_ids = shuffled[valid_count + test_count :]
    else:
        n_total = len(ordered)
        train_count = n_total - valid_count - test_count
        targets = {"valid": valid_count, "test": test_count, "train": train_count}
        buckets_by_key: list[tuple[tuple[int, int], list[str]]] = []
        for skey in sorted(strata.keys(), reverse=True):
            members = list(strata[skey])
            rng.shuffle(members)
            buckets_by_key.append((skey, members))

        alloc: dict[tuple[int, int], dict[str, int]] = {}
        used = {p: 0 for p in targets}
        for skey, members in buckets_by_key:
            size = len(members)
            alloc[skey] = {}
            for p, target in targets.items():
                floor_val = int(math.floor(size * target / n_total))
                alloc[skey][p] = floor_val
                used[p] += floor_val

        stratum_sizes = {skey: len(members) for skey, members in buckets_by_key}
        stratum_used = {skey: sum(alloc[skey].values()) for skey in stratum_sizes}
        part_priority = {"valid": 2, "test": 2, "train": 1}
        candidates: list[tuple[float, int, int, str, tuple[int, int]]] = []
        for skey, members in buckets_by_key:
            size = len(members)
            for p, target in targets.items():
                rem = (size * target / n_total) - alloc[skey][p]
                candidates.append((rem, skey[0], part_priority[p], p, skey))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)

        tie_flip = 0
        while any(used[p] < targets[p] for p in targets):
            progressed = False
            for _rem, _has_gold, _prio, p, skey in candidates:
                if used[p] < targets[p] and stratum_used[skey] < stratum_sizes[skey]:
                    chosen_p = p
                    if (
                        p in ("valid", "test")
                        and targets["valid"] == targets["test"]
                        and used["valid"] < targets["valid"]
                        and used["test"] < targets["test"]
                    ):
                        chosen_p = "valid" if (tie_flip % 2 == 0) else "test"
                        tie_flip += 1
                    alloc[skey][chosen_p] += 1
                    used[chosen_p] += 1
                    stratum_used[skey] += 1
                    progressed = True
                    if not any(used[q] < targets[q] for q in targets):
                        break
            if not progressed:
                for skey, _ in buckets_by_key:
                    for p in ("valid", "test", "train"):
                        while used[p] < targets[p] and stratum_used[skey] < stratum_sizes[skey]:
                            alloc[skey][p] += 1
                            used[p] += 1
                            stratum_used[skey] += 1

        valid_ids, test_ids, train_ids = [], [], []
        for skey, members in buckets_by_key:
            nv = alloc[skey]["valid"]
            nt = alloc[skey]["test"]
            valid_ids.extend(members[:nv])
            test_ids.extend(members[nv : nv + nt])
            train_ids.extend(members[nv + nt :])

    assignment: dict[str, str] = {}
    for pid in valid_ids:
        assignment[pid] = "valid"
    for pid in test_ids:
        assignment[pid] = "test"
    for pid in train_ids:
        assignment[pid] = "train"
    return assignment


def create_split(
    label_manifest: Sequence[LabelRecord | Mapping[str, Any]] | Mapping[str, Any],
    population_manifest: Any = None,
    *,
    seed: int | None = None,
    group_size: int = 5,
    stratify: bool = True,
    enforce_quotas: bool = True,
    bundles: Sequence[PublicationBundle] | None = None,
    min_manual_entries_per_pub: int = 20,
    require_full_auto_coverage: bool = False,
    population_snapshot_id: str | None = None,
    label_snapshot_id: str | None = None,
) -> PartitionManifest:
    """Create a deterministic publication-grouped PartitionManifest.

    Supports both contract call forms:
    - `create_split(label_manifest, seed)`
    - `create_split(label_manifest, population_manifest, *, seed=..., group_size=5)`
    """
    if type(population_manifest) is int and seed is None:
        seed = population_manifest
        population_manifest = None
    elif type(population_manifest) is bool:
        raise ContractError("population_manifest/seed must not be bool")

    if seed is None:
        seed = 42
    if type(seed) is not int or seed < 0:
        raise ContractError("seed must be a non-negative int")
    if type(group_size) is not int or group_size < 1:
        raise ContractError("group_size must be a positive int")

    manual_ids, auto_ids, labels, pub_stats, resolved_label_snap = _extract_label_pools(
        label_manifest, label_snapshot_id=label_snapshot_id
    )
    selected_ids = set(manual_ids) | set(auto_ids)
    population_count, resolved_pop_snap = _extract_population_info(
        population_manifest,
        selected_ids,
        population_snapshot_id=population_snapshot_id,
    )

    if enforce_quotas:
        min_manual = 5 * group_size
        min_auto = max(math.ceil((population_count - len(manual_ids)) / 10), 2 * group_size)
        if len(manual_ids) < min_manual:
            raise ContractError(
                f"Insufficient manual publications for group_size={group_size}: "
                f"got {len(manual_ids)}, need at least {min_manual}"
            )
        if len(auto_ids) < min_auto:
            raise ContractError(
                f"Insufficient auto publications for population_count={population_count}, "
                f"manual={len(manual_ids)}, group_size={group_size}: "
                f"got {len(auto_ids)}, need at least {min_auto}"
            )
        if bundles is not None and labels:
            bundle_map = {b.arxiv_id: b for b in bundles}
            for pid in manual_ids:
                sup_count = int(pub_stats.get(pid, {}).get("supervised", 0))
                if sup_count < min_manual_entries_per_pub:
                    raise ContractError(
                        f"Manual publication {pid} has only {sup_count} supervised entries; "
                        f"need at least {min_manual_entries_per_pub}"
                    )
            if require_full_auto_coverage:
                for pid in auto_ids:
                    if pid not in bundle_map:
                        raise ContractError(f"Auto publication {pid} missing from bundles")
                    total_bib = len(bundle_map[pid].bibtex)
                    sup_count = int(pub_stats.get(pid, {}).get("supervised", 0))
                    if sup_count < total_bib:
                        raise ContractError(
                            f"Auto publication {pid} has incomplete coverage: "
                            f"{sup_count}/{total_bib} supervised entries"
                        )

    rng = random.Random(seed)
    valid_m = min(group_size, len(manual_ids)) if not enforce_quotas else group_size
    test_m = min(group_size, max(0, len(manual_ids) - valid_m)) if not enforce_quotas else group_size
    valid_a = min(group_size, len(auto_ids)) if not enforce_quotas else group_size
    test_a = min(group_size, max(0, len(auto_ids) - valid_a)) if not enforce_quotas else group_size

    manual_assign = (
        _assign_pool_partitions(
            manual_ids,
            valid_count=valid_m,
            test_count=test_m,
            rng=rng,
            stats=pub_stats,
            stratify=stratify,
        )
        if manual_ids
        else {}
    )
    auto_assign = (
        _assign_pool_partitions(
            auto_ids,
            valid_count=valid_a,
            test_count=test_a,
            rng=rng,
            stats=pub_stats,
            stratify=stratify,
        )
        if auto_ids
        else {}
    )

    rows = [
        PartitionRow(arxiv_id=pid, partition=manual_assign[pid], pool="manual")
        for pid in manual_ids
    ] + [
        PartitionRow(arxiv_id=pid, partition=auto_assign[pid], pool="auto")
        for pid in auto_ids
    ]
    rows.sort(key=lambda r: r.arxiv_id)

    manifest = PartitionManifest(
        seed=seed,
        group_size=group_size,
        population_snapshot_id=resolved_pop_snap,
        label_snapshot_id=resolved_label_snap,
        manual_publication_ids=manual_ids,
        auto_publication_ids=auto_ids,
        rows=rows,
        schema_version=MATCHING_SCHEMA_VERSION,
    )

    if enforce_quotas:
        quota_report = validate_split_quotas(manifest, population_count=population_count)
        if not quota_report.valid:
            codes = [issue.code for issue in quota_report.errors]
            raise ContractError(f"Split failed quota validation: {codes}")

    assert_no_split_leakage(manifest, labels=labels if labels else None)
    return manifest


def assert_no_split_leakage(
    manifest: PartitionManifest,
    *,
    labels: Sequence[LabelRecord] | None = None,
    feature_batches: Mapping[str, FeatureBatch] | None = None,
    predictions: Sequence[RankedPrediction] | None = None,
    population_count: int | None = None,
) -> dict[str, Any]:
    """Assert zero publication leakage across partitions and pools.

    Raises ContractError on any overlap, pool mismatch, or cross-partition spill.
    """
    manual_set = set(manifest.manual_publication_ids)
    auto_set = set(manifest.auto_publication_ids)
    if manual_set & auto_set:
        raise ContractError(f"Manual/auto pools overlap: {sorted(manual_set & auto_set)}")

    by_partition: dict[str, set[str]] = {p: set() for p in PARTITIONS}
    pub_to_partition: dict[str, str] = {}
    pub_to_pool: dict[str, str] = {}

    for row in manifest.rows:
        if row.arxiv_id in pub_to_partition:
            raise ContractError(f"Publication {row.arxiv_id} assigned to multiple partitions")
        pub_to_partition[row.arxiv_id] = row.partition
        pub_to_pool[row.arxiv_id] = row.pool
        by_partition[row.partition].add(row.arxiv_id)

    for i, p1 in enumerate(PARTITIONS):
        for p2 in PARTITIONS[i + 1 :]:
            overlap = by_partition[p1] & by_partition[p2]
            if overlap:
                raise ContractError(f"Leakage between {p1} and {p2}: {sorted(overlap)}")

    if set(pub_to_partition) != manual_set | auto_set:
        raise ContractError("Partition rows do not match union of manual and auto pools")

    gold_coverage: dict[str, dict[str, int]] = {
        p: {
            "publications": len(by_partition[p]),
            "gold_publications": 0,
            "matched_queries": 0,
            "unmatched_queries": 0,
        }
        for p in PARTITIONS
    }

    if labels is not None:
        gold_pubs_by_part: dict[str, set[str]] = {p: set() for p in PARTITIONS}
        for lbl in labels:
            pid = lbl.arxiv_id
            if pid not in pub_to_pool:
                continue
            expected_source = "manual" if pub_to_pool[pid] == "manual" else "heuristic"
            if lbl.label_source != expected_source:
                raise ContractError(
                    f"Label provenance mismatch for {pid}: pool={pub_to_pool[pid]} "
                    f"but label_source={lbl.label_source}"
                )
            part = pub_to_partition[pid]
            if lbl.is_supervised:
                if lbl.decision == "matched":
                    gold_pubs_by_part[part].add(pid)
                    gold_coverage[part]["matched_queries"] += 1
                elif lbl.decision == "unmatched":
                    gold_coverage[part]["unmatched_queries"] += 1
        for p in PARTITIONS:
            gold_coverage[p]["gold_publications"] = len(gold_pubs_by_part[p])

    if feature_batches is not None:
        for part, batch in feature_batches.items():
            if part not in PARTITIONS:
                raise ContractError(f"Unknown feature batch partition: {part}")
            seen_pubs = {r.arxiv_id for r in batch.rows}
            leaked = sorted(seen_pubs - by_partition[part])
            if leaked:
                raise ContractError(
                    f"Feature leakage in partition '{part}': foreign publications {leaked}"
                )

    if predictions is not None:
        pred_parts: dict[str, set[str]] = defaultdict(set)
        for pred in predictions:
            pred_parts[pred.arxiv_id].add(pred.partition)
            expected_part = pub_to_partition.get(pred.arxiv_id)
            if expected_part is None:
                raise ContractError(f"Prediction publication {pred.arxiv_id} outside split")
            if pred.partition != expected_part:
                raise ContractError(
                    f"Prediction partition mismatch for {pred.arxiv_id}: "
                    f"got {pred.partition}, expected {expected_part}"
                )
        multi = sorted(pid for pid, parts in pred_parts.items() if len(parts) > 1)
        if multi:
            raise ContractError(f"Predictions span multiple partitions for publications: {multi}")

    if population_count is not None:
        report: ValidationReport = validate_split_quotas(manifest, population_count=population_count)
        if not report.valid:
            raise ContractError(f"Split quota violation: {[e.code for e in report.errors]}")

    return {
        "split_id": manifest.split_id,
        "counts": manifest.counts,
        "gold_coverage": gold_coverage,
        "disjoint_verified": True,
    }


def save_partition_manifest(manifest: PartitionManifest, output_path: str | Path) -> Path:
    """Persist PartitionManifest atomically as UTF-8 JSON."""
    path = Path(output_path)
    atomic_json(path, manifest.to_dict())
    return path


def load_partition_manifest(input_path: str | Path) -> PartitionManifest:
    """Load and validate a PartitionManifest from JSON."""
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractError("PartitionManifest JSON must be an object")
    rows = [PartitionRow(**r) for r in raw["rows"]]
    manifest = PartitionManifest(
        seed=raw["seed"],
        group_size=raw["group_size"],
        population_snapshot_id=raw["population_snapshot_id"],
        label_snapshot_id=raw["label_snapshot_id"],
        manual_publication_ids=raw["manual_publication_ids"],
        auto_publication_ids=raw["auto_publication_ids"],
        rows=rows,
        schema_version=raw.get("schema_version", MATCHING_SCHEMA_VERSION),
    )
    if "split_id" in raw and raw["split_id"] != manifest.split_id:
        raise ContractError("PartitionManifest split_id does not match content hash")
    return manifest
