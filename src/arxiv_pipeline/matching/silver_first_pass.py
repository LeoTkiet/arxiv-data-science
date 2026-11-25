"""Resumable first heuristic pass over verified T3-05 prepared bundles."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from dataclasses import asdict

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.contracts import ContractError, LabelRecord, publication_from_rows
from arxiv_pipeline.matching.heuristics import (
    HeuristicPolicy, auto_label, extract_author_last_names, normalize_title,
    title_similarity,
)
from arxiv_pipeline.matching.labeling import validate_label_manifest
from arxiv_pipeline.reporting.milestone2.validation import safe_path


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_context(bundle, label, member: str) -> dict:
    bib = next(item for item in bundle.bibtex if item.citation_key == label.citation_key)
    title = normalize_title(bib.title)
    surnames = extract_author_last_names(bib.authors)
    candidates = []
    for ref in bundle.references:
        ref_surnames = extract_author_last_names(ref.ref_authors)
        candidates.append(dict(
            ref_arxiv_id=ref.ref_arxiv_id, ref_title=ref.ref_title,
            ref_authors=ref.ref_authors, ref_submission_date=ref.ref_submission_date,
            title_similarity=round(title_similarity(title, normalize_title(ref.ref_title)), 4),
            author_overlap=sorted(surnames & ref_surnames),
        ))
    candidates.sort(key=lambda item: (-item["title_similarity"], item["ref_arxiv_id"]))
    score = candidates[0]["title_similarity"] if candidates else 0.0
    if label.decision != "uncertain":
        cause = "accepted_rule"
    elif label.notes != "rule=abstain_ambiguous":
        cause = label.notes.removeprefix("rule=")
    elif not title:
        cause = "missing_bib_title"
    elif any(not ref.ref_title for ref in bundle.references):
        cause = "missing_reference_title"
    elif score >= 0.5:
        cause = "moderate_title_similarity"
    elif score >= 0.25:
        cause = "related_title_below_match_threshold"
    elif candidates and candidates[0]["author_overlap"]:
        cause = "low_title_similarity_with_author_overlap"
    else:
        cause = "other_abstention"
    return dict(arxiv_id=bundle.arxiv_id, member=member,
                citation_key=label.citation_key, decision=label.decision,
                rule=label.notes, confidence=label.confidence, cause=cause,
                bib_title=bib.title, bib_authors=bib.authors, bib_year=bib.year,
                bib_doi=bib.doi, bib_url=bib.url, candidates=candidates[:3])


def run_first_pass(prepared_path: Path, population_path: Path, output: Path,
                   *, progress_every: int = 100) -> dict:
    """Generate draft labels; never treat abstentions as accepted or handoff ready."""
    prepared_path, population_path, output = Path(prepared_path), Path(population_path), Path(output)
    prepared, population = _read(prepared_path), _read(population_path)
    if prepared.get("task") != "T3-05" or not isinstance(prepared.get("publications"), list):
        raise ContractError("Expected T3-05 prepared_manifest.json")
    population_ids = set(population.get("publication_ids", []))
    manual_ids = set(population.get("excluded_manual_ids", []))
    if not population_ids or not manual_ids or manual_ids - population_ids:
        raise ContractError("Invalid population/manual IDs")
    if population.get("population_count") != len(population_ids):
        raise ContractError("Population count disagrees with publication_ids")
    rows = prepared["publications"]
    if prepared.get("total") != len(rows):
        raise ContractError("Prepared manifest total disagrees with rows")
    ready = [row for row in rows if row.get("status") == "ready"]
    if prepared.get("ready") != len(ready):
        raise ContractError("Prepared manifest ready count disagrees with rows")
    keys = [row.get("arxiv_id") for row in rows]
    if len(keys) != len(set(keys)) or set(keys) - population_ids or set(keys) & manual_ids:
        raise ContractError("Prepared publications are duplicated, outside population, or manual")
    if type(progress_every) is not int or progress_every < 1:
        raise ContractError("progress_every must be a positive integer")

    policy = HeuristicPolicy(accept_clear_decisions=True)
    policy_config = asdict(policy)
    policy_key = hashlib.sha256(json.dumps(policy_config, sort_keys=True).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    labels: list[LabelRecord] = []
    uncertain = []
    uncertain_details = []
    spot_check_candidates: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    uncertain_reason_counts: dict[str, int] = {}
    publications = []
    total_entries = 0
    candidate_full_coverage = 0
    full_with_references = 0
    full_without_references = 0
    full_with_match = 0
    rule_counts: dict[str, int] = {}
    decision_counts = {"matched": 0, "unmatched": 0, "uncertain": 0}

    for index, row in enumerate(ready, 1):
        paper = row["arxiv_id"]
        bundle_path = safe_path(prepared_path.parent, row["bundle"])
        if _sha256(bundle_path) != row.get("sha256"):
            raise ContractError(f"Prepared bundle hash mismatch: {paper}")
        raw = _read(bundle_path)
        if raw.get("arxiv_id") != paper or raw.get("readiness") != "ready":
            raise ContractError(f"Prepared bundle identity/readiness mismatch: {paper}")
        bundle = publication_from_rows(paper, raw["bibtex"], raw["references"], readiness="ready")
        target = output / "per_publication" / row["member"] / f"{paper}.json"
        receipt = target.with_suffix(".receipt.json")
        cached = False
        if target.is_file() and receipt.is_file():
            prior = _read(receipt)
            cached = (prior.get("bundle_sha256") == row["sha256"]
                      and prior.get("policy") == policy_key
                      and prior.get("labels_sha256") == _sha256(target))
        if cached:
            paper_labels = [LabelRecord(**item) for item in _read(target)]
        else:
            paper_labels = auto_label([bundle], policy)
            atomic_json(target, [item.to_dict() for item in paper_labels])
            atomic_json(receipt, dict(arxiv_id=paper, bundle_sha256=row["sha256"],
                                      policy=policy_key, labels_sha256=_sha256(target)))
        report = validate_label_manifest([bundle], paper_labels)
        if not report.valid or report.counts["unlabeled"] or report.counts["labeled"] != len(bundle.bibtex):
            raise ContractError(f"First-pass labels failed query/FK validation: {paper}")
        labels.extend(paper_labels)
        total_entries += len(bundle.bibtex)
        reference_count = len(bundle.references)
        if row.get("reference_count", reference_count) != reference_count:
            raise ContractError(f"Prepared reference count mismatch: {paper}")
        accepted = sum(label.is_supervised for label in paper_labels)
        abstain = sum(label.decision == "uncertain" for label in paper_labels)
        matched = sum(label.decision == "matched" for label in paper_labels)
        full = bool(bundle.bibtex) and accepted == len(bundle.bibtex) and abstain == 0
        candidate_full_coverage += full
        full_with_references += bool(full and reference_count)
        full_without_references += bool(full and not reference_count)
        full_with_match += bool(full and matched)
        publications.append(dict(arxiv_id=paper, member=row["member"], bibtex_count=len(bundle.bibtex),
                                 reference_count=reference_count, accepted=accepted, matched=matched,
                                 uncertain=abstain, candidate_full_coverage=full))
        for label in paper_labels:
            decision_counts[label.decision] += 1
            rule = label.notes or "rule=unknown"
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
            if label.decision == "uncertain":
                uncertain.append(dict(arxiv_id=paper, citation_key=label.citation_key,
                                      confidence=label.confidence, rule=rule, member=row["member"],
                                      bundle=row["bundle"]))
                detail = _review_context(bundle, label, row["member"])
                uncertain_details.append(detail)
                cause = detail["cause"]
                uncertain_reason_counts[cause] = uncertain_reason_counts.get(cause, 0) + 1
            else:
                bucket = row["member"], rule
                rank = hashlib.sha256(f'{paper}:{label.citation_key}'.encode()).hexdigest()
                current = spot_check_candidates.setdefault(bucket, [])
                if len(current) < 20 or rank < current[-1][0]:
                    current.append((rank, _review_context(bundle, label, row["member"])))
                    current.sort(key=lambda item: item[0])
                    del current[20:]
        if index % progress_every == 0 or index == len(ready):
            print(f"Heuristic: {index}/{len(ready)} ready publications; "
                  f"full coverage candidates: {candidate_full_coverage}; uncertain: {len(uncertain)}")

    atomic_json(output / "draft_labels.json", [label.to_dict() for label in labels])
    atomic_json(output / "uncertain_rows.json", uncertain)
    atomic_json(output / "uncertain_details.json", uncertain_details)
    sample = [item for bucket in sorted(spot_check_candidates)
              for _, item in spot_check_candidates[bucket]]
    atomic_json(output / "spot_check_sample.json", sample)
    minimum = max(math.ceil((len(population_ids) - len(manual_ids)) * 0.1), 10)
    summary = dict(task="T3-05", phase="heuristic_first_pass", policy=policy_config,
                   policy_sha256=policy_key,
                   prepared_manifest_sha256=_sha256(prepared_path),
                   population_snapshot_id=population.get("population_snapshot_id"),
                   population_count=len(population_ids), manual_count=len(manual_ids),
                   minimum_auto_publications=minimum, prepared_ready=len(ready),
                   total_entries=total_entries, total_labels=len(labels),
                   candidate_full_coverage_publications=candidate_full_coverage,
                   full_coverage_with_references=full_with_references,
                   full_coverage_without_references=full_without_references,
                   full_coverage_with_match=full_with_match,
                   uncertain_entries=len(uncertain), uncertain_reason_counts=uncertain_reason_counts,
                   spot_check_sample_count=len(sample), spot_check_sample_target=100,
                   spot_check_sample_method="deterministic random sample stratified by member and rule; up to 20 per stratum",
                   decision_counts=decision_counts,
                   rule_counts=rule_counts, publications=publications,
                   ready_for_handoff=False,
                   next_step="Resolve uncertain rows, spot-check accepted rules, then finalize and validate the auto pool")
    atomic_json(output / "first_pass_manifest.json", summary)
    return summary
