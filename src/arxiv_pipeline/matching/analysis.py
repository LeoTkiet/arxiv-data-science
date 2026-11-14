"""TV4 error analysis (T4-04): ablation, importance, slices, error cases.

Everything here runs on train/valid only; test stays untouched until the model
config is frozen. Runs on mock data today:
    python -m arxiv_pipeline.matching.analysis --mock

TODO(T4-04, blocked on real data):
- Replace mock feature groups with TV2's feature_schema.json groups (T2-03).
- Swap mrr_at_5 for TV5's official evaluator (T5-02) and confirm how labeled
  "unmatched" entries count in MRR@5.
- Add slices on real metadata: missing DOI/title, entry_type, publication size,
  manual vs auto pool, hierarchy depth (if features use M2 hierarchy).
- Error taxonomy needs bibtex/reference text joined back by key (T1-02) so
  error cases can be read, not just counted.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter, defaultdict

from arxiv_pipeline.matching.contracts import FeatureBatch, PairFeature
from arxiv_pipeline.matching.model import fit_model, mrr_at_5, predict_scores
from arxiv_pipeline.matching.train import (
    mock_dataset, partition_batch, targets_from_labels,
)

# TODO(T2-03): read groups from TV2's feature schema instead of this mock map.
MOCK_GROUPS = {
    "title": ["title_jaccard", "title_char_sim", "title_missing"],
    "author": ["author_overlap"],
    "year": ["year_abs_diff"],
    "identifier": ["has_doi_match"],
}


def _subset(batch: FeatureBatch, keep: list[str]) -> FeatureBatch:
    rows = [PairFeature(r.arxiv_id, r.citation_key, r.ref_arxiv_id,
                        {k: r.values[k] for k in keep}) for r in batch.rows]
    return FeatureBatch(rows, f"{batch.feature_schema_id}:{','.join(keep)}")


def _rank_of_gold(scores, targets):
    """Per labeled-positive query: 1-based rank of the gold candidate (or None)."""
    by_q = defaultdict(list)
    for s in scores:
        by_q[(s.arxiv_id, s.citation_key)].append(s)
    out = {}
    for k, y in targets.items():
        if y == 1:
            ranked = sorted(by_q[k[:2]], key=lambda s: (-s.score, s.ref_arxiv_id))
            ids = [s.ref_arxiv_id for s in ranked]
            out[k[:2]] = (ids.index(k[2]) + 1, ranked)
    return out


def ablation(train, train_t, valid, valid_t, config, groups=MOCK_GROUPS):
    """Drop one feature group at a time; report valid MRR@5 delta vs all features."""
    cols = list(train.rows[0].values)
    full = mrr_at_5(predict_scores(fit_model(train, train_t, valid, valid_t, config), valid), valid_t)
    rows = [dict(dropped=None, valid_mrr_at_5=full, delta=0.0)]
    for name, group in groups.items():
        keep = [c for c in cols if c not in group]
        if not keep:
            continue
        tr, va = _subset(train, keep), _subset(valid, keep)
        m = mrr_at_5(predict_scores(fit_model(tr, train_t, va, valid_t, config), va), valid_t)
        rows.append(dict(dropped=name, valid_mrr_at_5=m, delta=m - full))
    return rows


def permutation_importance(artifact, valid, valid_t, *, repeats=5, seed=0):
    """MRR@5 drop when one column is shuffled across valid rows (model-agnostic)."""
    rng = random.Random(seed)
    base = mrr_at_5(predict_scores(artifact, valid), valid_t)
    out = {}
    for col in artifact.feature_columns:
        drops = []
        for _ in range(repeats):
            vals = [r.values[col] for r in valid.rows]
            rng.shuffle(vals)
            rows = [PairFeature(r.arxiv_id, r.citation_key, r.ref_arxiv_id, {**r.values, col: v})
                    for r, v in zip(valid.rows, vals)]
            drops.append(base - mrr_at_5(predict_scores(artifact, FeatureBatch(rows, valid.feature_schema_id)), valid_t))
        out[col] = sum(drops) / repeats
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def slices(artifact, valid, valid_t, labels):
    """MRR@5 per slice of the gold pair: title missing, DOI known, label pool."""
    ranks = _rank_of_gold(predict_scores(artifact, valid), valid_t)
    gold = {k: r for k, r in ((row.key, row) for row in valid.rows) if valid_t.get(k) == 1}
    source = {l.key: l.label_source for l in labels}
    buckets = defaultdict(list)
    for key, row in gold.items():
        rr = ranks[key[:2]][0]
        score = 1 / rr if rr <= 5 else 0.0
        buckets[f"title_missing={bool(row.values.get('title_missing'))}"].append(score)
        buckets[f"doi_known={row.values.get('has_doi_match') is not None}"].append(score)
        buckets[f"label_source={source.get(key[:2])}"].append(score)
    return {k: dict(n=len(v), mrr_at_5=sum(v) / len(v)) for k, v in sorted(buckets.items())}


def error_cases(artifact, valid, valid_t, *, limit=20):
    """Every query whose gold is not ranked first, with a coarse reason tag.

    Reported systematically (all errors counted, examples listed in key order),
    never cherry-picked. TODO(T1-02): attach bibtex/reference text for reading.
    """
    ranks = _rank_of_gold(predict_scores(artifact, valid), valid_t)
    feats = {r.key: r.values for r in valid.rows}
    cases, tags = [], Counter()
    for q, (rank, ranked) in sorted(ranks.items()):
        if rank == 1:
            continue
        top = ranked[0]
        gold_key = next(k for k, y in valid_t.items() if y == 1 and k[:2] == q)
        g, t = feats[gold_key], feats[top.key]
        if g.get("title_missing"):
            tag = "gold_title_missing"
        elif (t.get("title_jaccard") or 0) >= (g.get("title_jaccard") or 0):
            tag = "near_duplicate_title"
        else:
            tag = "other_signal_outweighed"
        tags[tag] += 1
        cases.append(dict(arxiv_id=q[0], citation_key=q[1], gold_rank=rank,
                          top1=top.ref_arxiv_id, gold=gold_key[2], tag=tag))
    return dict(n_errors=len(cases), n_queries=len(ranks), tags=dict(tags), examples=cases[:limit])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mock", action="store_true", required=True,
                    help="Only synthetic mode until real data is handed over (TODO T4-04)")
    ap.add_argument("--kind", default="logreg")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    batch, labels, manifest = mock_dataset(args.seed)
    tr_all, va_all = (partition_batch(batch, manifest, p) for p in ("train", "valid"))
    tr_t, va_t = targets_from_labels(tr_all, labels), targets_from_labels(va_all, labels)
    tr = FeatureBatch([r for r in tr_all.rows if r.key in tr_t], batch.feature_schema_id)
    va = FeatureBatch([r for r in va_all.rows if r.key in va_t], batch.feature_schema_id)
    config = dict(kind=args.kind, seed=args.seed)
    art = fit_model(tr, tr_t, va, va_t, config)
    out = dict(note="SYNTHETIC MOCK DATA - not experimental results", model_id=art.model_id,
               ablation=ablation(tr, tr_t, va, va_t, config),
               permutation_importance=permutation_importance(art, va, va_t, seed=args.seed),
               slices=slices(art, va, va_t, labels),
               errors=error_cases(art, va, va_t, limit=5))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
