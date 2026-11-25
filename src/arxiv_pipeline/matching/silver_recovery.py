"""Recover missing query metadata from verified MS2 bibliography sidecars."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.matching.contracts import ContractError
from arxiv_pipeline.matching.heuristics import extract_arxiv_ids, normalize_title
from arxiv_pipeline.matching.io import ZipSnapshot, ms2_snapshot_path
from arxiv_pipeline.matching.silver_first_pass import run_first_pass
from arxiv_pipeline.parsing.bibliography import parse_bibitem
from arxiv_pipeline.reporting.milestone2.validation import safe_path


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _source_items(archive, paper, versions):
    """Read only artifacts declared and hashed by the bibliography checkpoint."""
    items = defaultdict(list)
    for version in versions:
        checkpoint = f"state/stages/bibliography/{paper_key(paper)}/{version}.json"
        raw_checkpoint = json.loads(archive.read_bytes(checkpoint).decode("utf-8-sig"))
        if (raw_checkpoint.get("paper_id"), raw_checkpoint.get("version"),
                raw_checkpoint.get("schema_version")) != (paper, version, "2.0"):
            raise ContractError(f"Wrong bibliography checkpoint identity: {checkpoint}")
        if raw_checkpoint.get("status") not in {"success", "no_source"}:
            raise ContractError(f"Bibliography not complete: {checkpoint}")
        artifacts = raw_checkpoint["artifacts"]
        dependencies = json.loads(archive.read_bytes(
            checkpoint.replace(".json", ".dependencies.json")).decode("utf-8-sig"))
        inventory = dependencies["artifacts"]
        if set(inventory) != set(artifacts.values()):
            raise ContractError(f"Bibliography inventory mismatch: {checkpoint}")
        for relative in artifacts.values():
            content = archive.read_bytes(relative)
            if hashlib.sha256(content).hexdigest() != inventory[relative]:
                raise ContractError(f"Bibliography hash mismatch: {relative}")
        if "provenance" not in artifacts:
            continue
        relative = artifacts["provenance"]
        side = json.loads(archive.read_bytes(relative).decode("utf-8-sig"))
        if (side.get("arxiv_id"), side.get("version"), side.get("schema_version")) != (paper, version, "2.0"):
            raise ContractError(f"Wrong bibliography sidecar identity: {relative}")
        for item in side.get("items", []):
            if item.get("citation_key") and item.get("raw_text"):
                items[item["citation_key"]].append(dict(
                    version=version, artifact=relative,
                    sha256=inventory[relative], raw_text=item["raw_text"]))
    return items


def recover_query(query, sources):
    """Use literal identifiers and explicitly tagged titles, requiring agreement."""
    recovered = dict(query)
    changes = {}
    identifiers = set()
    for field in (query.get("doi"), query.get("url"), query.get("journal"), query["citation_key"]):
        identifiers.update(extract_arxiv_ids(field))
    for source in sources:
        identifiers.update(extract_arxiv_ids(source["raw_text"]))
    # Populate a missing field only. Multiple identifiers remain unresolved.
    if len(identifiers) == 1 and not query.get("url"):
        recovered["url"] = "https://arxiv.org/abs/" + next(iter(identifiers))
        changes["url"] = recovered["url"]
    titles = {}
    for source in sources:
        fields, _ = parse_bibitem(source["raw_text"])
        if fields and fields.get("title"):
            titles[normalize_title(fields["title"])] = fields["title"]
        # RevTeX bibinfo is an explicit field, unlike arbitrary italicized text.
        for title in re.findall(r"\\bibinfo\s*\{title\}\s*\{([^{}]+)\}", source["raw_text"]):
            titles[normalize_title(title)] = title
    titles.pop("", None)
    if not query.get("title") and len(titles) == 1:
        recovered["title"] = next(iter(titles.values()))
        changes["title"] = recovered["title"]
    return recovered, changes


def run_recovery(prepared_path, population_path, roots_path, baseline_path, output):
    """Stage enriched copies, label them, and count publications fully resolved."""
    prepared_path, output = Path(prepared_path), Path(output)
    original, roots = _read(prepared_path), _read(roots_path)
    baseline_path = Path(baseline_path)
    baseline = _read(baseline_path / "first_pass_manifest.json")
    if baseline["prepared_manifest_sha256"] != _hash(prepared_path):
        raise ContractError("Baseline belongs to a different prepared snapshot")
    baseline_labels = _read(baseline_path / "draft_labels.json")
    before = {(row["arxiv_id"], row["citation_key"]): row for row in baseline_labels}
    stage = output / "prepared"
    records, evidence, errors = [], [], []
    for member in sorted(roots):
        rows = [row for row in original["publications"] if row["member"] == member]
        with ZipSnapshot(ms2_snapshot_path(Path(roots[member]["ms2_root"]))) as archive:
            for index, row in enumerate(rows, 1):
                if row["status"] != "ready":
                    records.append(row)
                    continue
                path = safe_path(prepared_path.parent, row["bundle"])
                if _hash(path) != row["sha256"]:
                    raise ContractError(f"Bundle hash mismatch: {row['arxiv_id']}")
                bundle = _read(path)
                paper = row["arxiv_id"]
                sources = {}
                versions = row.get("provenance", {}).get("files", {})
                # Version list comes from the verified preparation input manifest.
                relative = f"tables/{paper_key(paper)}/parsing_manifest.json"
                try:
                    content = archive.read_bytes(relative)
                    if hashlib.sha256(content).hexdigest() != versions.get("ms2/" + relative):
                        raise ContractError("MS2 manifest differs from prepared source")
                    manifest = json.loads(content.decode("utf-8-sig"))
                    sources = _source_items(archive, paper, manifest["expected_versions"])
                except (FileNotFoundError, KeyError, ValueError, UnicodeError) as exc:
                    errors.append(dict(arxiv_id=paper, member=member, error=str(exc)))
                    sources = {}
                for i, query in enumerate(bundle["bibtex"]):
                    prior = before[(paper, query["citation_key"])]
                    if prior["decision"] != "uncertain":
                        continue
                    source = sources.get(query["citation_key"], [])
                    recovered, changes = recover_query(query, source)
                    if changes:
                        bundle["bibtex"][i] = recovered
                        evidence.append(dict(arxiv_id=paper, citation_key=query["citation_key"],
                                             member=member, changes=changes, sources=source))
                target = stage / "bundles" / member / f"{paper_key(paper)}.json"
                atomic_json(target, bundle)
                records.append(dict(row, bundle=target.relative_to(stage).as_posix(),
                                    sha256=_hash(target), original_bundle_sha256=row["sha256"]))
                if index % 100 == 0:
                    print(f"Recovery {member}: {index}/{len(rows)}; enriched queries: {len(evidence)}", flush=True)
    staged = dict(original, publications=records)
    atomic_json(stage / "prepared_manifest.json", staged)
    atomic_json(output / "recovery_evidence.json", evidence)
    atomic_json(output / "recovery_errors.json", errors)
    summary = run_first_pass(stage / "prepared_manifest.json", population_path, output / "first_pass")
    after = _read(output / "first_pass/draft_labels.json")
    changes = [dict(before=before[(row["arxiv_id"], row["citation_key"])], after=row)
               for row in after if (row["decision"], row["groundtruth_arxiv_id"]) !=
               (before[(row["arxiv_id"], row["citation_key"])]["decision"],
                before[(row["arxiv_id"], row["citation_key"])]["groundtruth_arxiv_id"])]
    atomic_json(output / "changed_labels.json", changes)
    comparison = dict(baseline_full=baseline["candidate_full_coverage_publications"],
                      recovered_full=summary["candidate_full_coverage_publications"],
                      recovered_with_references=summary["full_coverage_with_references"],
                      recovered_with_match=summary["full_coverage_with_match"],
                      baseline_uncertain=baseline["uncertain_entries"],
                      recovered_uncertain=summary["uncertain_entries"],
                      enriched_queries=len(evidence), source_errors=len(errors),
                      changed_decisions=len(changes), ready_for_handoff=False)
    comparison["additional_full_publications"] = comparison["recovered_full"] - comparison["baseline_full"]
    comparison["quota_shortfall"] = max(0, summary["minimum_auto_publications"] - comparison["recovered_full"])
    atomic_json(output / "comparison.json", comparison)
    return comparison
