"""TV4 training driver: targets from labels, split-aware training, model comparison.

Run the synthetic smoke experiment (no real data, numbers are NOT results):
    python -m arxiv_pipeline.matching.train --mock --out artifacts/ms3_mock
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random

from arxiv_pipeline.matching.contracts import (
    ContractError, FeatureBatch, LabelRecord, PairFeature, PairKey, PartitionManifest,
    PartitionRow,
)
from arxiv_pipeline.matching.model import (
    MODEL_KINDS, fit_model, mrr_at_5, predict_scores, save_artifact,
)


# ------------------------------------------------------------------- targets/split
def targets_from_labels(batch: FeatureBatch, labels: list[LabelRecord], *,
                        allow_sources=("manual", "heuristic")) -> dict[PairKey, int]:
    """Pair targets for supervised queries only.

    matched -> 1 for the groundtruth pair, 0 for other candidates of that query;
    unmatched -> all 0; uncertain / unreviewed / unlabeled -> excluded.
    """
    usable = {l.key: l for l in labels if l.is_supervised and l.label_source in allow_sources}
    return {row.key: int(row.ref_arxiv_id == usable[row.key[:2]].groundtruth_arxiv_id)
            for row in batch.rows if row.key[:2] in usable}


def partition_batch(batch: FeatureBatch, manifest: PartitionManifest, partition: str,
                    *, only_keys=None) -> FeatureBatch:
    pubs = {r.arxiv_id for r in manifest.rows if r.partition == partition}
    rows = [r for r in batch.rows if r.arxiv_id in pubs
            and (only_keys is None or r.key in only_keys)]
    return FeatureBatch(rows, batch.feature_schema_id)


def train_and_compare(batch: FeatureBatch, labels: list[LabelRecord],
                      manifest: PartitionManifest, *, kinds=("logreg", "hgb"),
                      seed=0, neg_per_query=None, use_silver=True, evaluator=mrr_at_5):
    """Fit each model family on train, tune on valid; never reads test.

    Returns (best_artifact, report). Model choice uses valid MRR@5 only; test is
    scored later by TV5 after the config is frozen.
    """
    sources = ("manual", "heuristic") if use_silver else ("manual",)
    train_t = targets_from_labels(partition_batch(batch, manifest, "train"), labels,
                                  allow_sources=sources)
    # Validation keeps manual + auto pools exactly as split by TV5.
    valid_t = targets_from_labels(partition_batch(batch, manifest, "valid"), labels)
    train_b = partition_batch(batch, manifest, "train", only_keys=train_t)
    valid_b = partition_batch(batch, manifest, "valid", only_keys=valid_t)
    provenance = dict(split_id=manifest.split_id, label_snapshot_id=manifest.label_snapshot_id,
                      use_silver=use_silver)
    report, best = [], None
    for kind in kinds:
        art = fit_model(train_b, train_t, valid_b, valid_t,
                        dict(kind=kind, seed=seed, neg_per_query=neg_per_query,
                             provenance=provenance, evaluator=evaluator))
        row = dict(kind=kind, model_id=art.model_id, params=art.params,
                   train_mrr_at_5=evaluator(predict_scores(art, train_b), train_t),
                   valid_mrr_at_5=max(s["valid_mrr_at_5"] for s in art.selection))
        report.append(row)
        if best is None or row["valid_mrr_at_5"] > best[0]:
            best = (row["valid_mrr_at_5"], art)
    return best[1], report


# ------------------------------------------------------------- synthetic mock data
MOCK_COLUMNS = ("title_jaccard", "title_char_sim", "author_overlap",
                "year_abs_diff", "has_doi_match", "title_missing")


def mock_dataset(seed=0, n_manual=25, n_auto=12, queries=(20, 30), refs=(15, 35)):
    """Synthetic pairs with contract types. Signal is planted so learning is testable.

    Hard negatives (near-duplicate titles), unmatched queries (~15%), missing
    values and a few noisy silver labels are included. Not real data.
    """
    rng = random.Random(seed)
    rows, labels, pubs = [], [], []
    ref_counter = 0
    for p in range(n_manual + n_auto):
        pub = f"2301.{p + 1:05d}"
        pubs.append(pub)
        is_auto = p >= n_manual
        n_refs = rng.randint(*refs)
        ref_ids = [f"2001.{ref_counter + i + 1:05d}" for i in range(n_refs)]
        ref_counter += n_refs
        targets = rng.sample(ref_ids, min(n_refs, rng.randint(*queries)))
        for q, target in enumerate(targets):
            key = f"ref{q}"
            matched = rng.random() > 0.15
            gold = target if matched else None
            hard = rng.sample([r for r in ref_ids if r != target], k=min(2, n_refs - 1))
            for ref in ref_ids:
                pos = ref == gold
                near = ref in hard
                t_missing = rng.random() < 0.08
                base = rng.betavariate(8, 2) if pos else (rng.betavariate(5, 3) if near
                                                          else rng.betavariate(1.5, 6))
                ydiff = (0 if rng.random() < 0.8 else rng.randint(1, 2)) if pos else \
                        (rng.randint(0, 3) if near else rng.randint(0, 15))
                vals = {
                    "title_jaccard": None if t_missing else round(base, 4),
                    "title_char_sim": None if t_missing else round(min(1, base + rng.gauss(0, .07)) if base < 1 else 1.0, 4),
                    "author_overlap": None if rng.random() < .1 else
                        round(rng.betavariate(6, 2) if pos else rng.betavariate(1.5, 5), 4),
                    "year_abs_diff": None if rng.random() < .1 else ydiff,
                    "has_doi_match": None if rng.random() < .7 else (pos and rng.random() < .9),
                    "title_missing": t_missing,
                }
                vals["title_char_sim"] = None if t_missing else max(0.0, vals["title_char_sim"])
                rows.append(PairFeature(pub, key, ref, vals))
            if is_auto:
                noisy = rng.random() < 0.05
                g = rng.choice(ref_ids) if (noisy and gold) else gold
                labels.append(LabelRecord(pub, key, g, "matched" if g else "unmatched",
                                          "heuristic", confidence=0.9,
                                          heuristic_version="mock-rule-1",
                                          review_status="accepted"))
            else:
                if rng.random() < 0.03:
                    labels.append(LabelRecord(pub, key, None, "uncertain", "manual",
                                              annotator="mock"))
                    continue
                labels.append(LabelRecord(pub, key, gold, "matched" if gold else "unmatched",
                                          "manual", annotator="mock", reviewer="mock",
                                          review_status="accepted"))
    manual, auto = pubs[:n_manual], pubs[n_manual:]
    rm, ra = manual[:], auto[:]
    rng.shuffle(rm)
    rng.shuffle(ra)
    part = {}
    for pool in (rm, ra):
        for i, pid in enumerate(pool):
            part[pid] = "test" if i < 5 else "valid" if i < 10 else "train"
    manifest = PartitionManifest(seed, 5, "mock-population", "mock-labels-1", manual, auto,
                                 [PartitionRow(pid, part[pid], "manual" if pid in manual else "auto")
                                  for pid in pubs])
    return FeatureBatch(rows, "mock-schema-1"), labels, manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mock", action="store_true", required=True,
                    help="Only synthetic mode exists until TV2/TV3/TV5 hand over real data")
    ap.add_argument("--out", default="artifacts/ms3_mock")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--kinds", nargs="+", default=["logreg", "rf", "hgb", "lambdamart"],
                    choices=MODEL_KINDS)
    args = ap.parse_args(argv)
    batch, labels, manifest = mock_dataset(args.seed)
    best, report = train_and_compare(batch, labels, manifest, kinds=args.kinds, seed=args.seed)
    path = save_artifact(best, Path(args.out) / "models")
    out = dict(note="SYNTHETIC MOCK DATA - not experimental results",
               split_counts=manifest.counts, models=report, selected=best.model_id,
               artifact=str(path))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
