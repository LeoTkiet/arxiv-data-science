"""Rank additional T3-05 publications by observable MS2 BibTeX completeness."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from arxiv_pipeline.common.utils import atomic_json, paper_key
from arxiv_pipeline.matching.contracts import ContractError, canonical_id
from arxiv_pipeline.matching.io import ZipSnapshot, ms2_snapshot_path


def _read(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def scan_candidates(population_path: Path, selection_path: Path, roots_path: Path,
                    ranges_path: Path, output: Path, *, pilot_per_member: int = 100,
                    seed: int = 42) -> dict:
    """Scan unselected BibTeX metadata and create a quality-enriched pilot.

    The pilot is diagnostic; its IDs are not automatically accepted labels.
    """
    population, selection, roots = _read(population_path), _read(selection_path), _read(roots_path)
    assigned = _read(ranges_path).get("ranges")
    if not isinstance(assigned, list) or len(assigned) != 5 or set(roots) != {f"tv{i}" for i in range(1, 6)}:
        raise ContractError("Expected five assigned ranges and five MS1/MS2 roots")
    if type(pilot_per_member) is not int or pilot_per_member < 1:
        raise ContractError("pilot_per_member must be a positive integer")
    ids = [canonical_id(p) for p in population.get("publication_ids", [])]
    manual = {canonical_id(p) for p in population.get("excluded_manual_ids", [])}
    if len(ids) != len(set(ids)) or not manual <= set(ids):
        raise ContractError("Invalid population/manual IDs")
    selected = {p for papers in selection.get("selected", {}).values() for p in papers}
    if selection.get("task") != "T3-05" or not selected <= set(ids) or selected & manual:
        raise ContractError("Invalid existing T3-05 selection")
    bounds = {f"tv{i+1}": tuple(row["range"].split(":")) for i, row in enumerate(assigned)}
    by_member = {member: [] for member in bounds}
    for paper in ids:
        if paper in manual or paper in selected:
            continue
        owners = [m for m, (start, end) in bounds.items() if start <= paper <= end]
        if len(owners) != 1:
            raise ContractError(f"No unique owner for {paper}")
        by_member[owners[0]].append(paper)

    report = dict(task="T3-05", phase="additional_candidate_scan", seed=seed,
                  selection_method="pilot ranked by BibTeX title/author completeness and entry count",
                  source_selection_count=len(selected), members={}, scan_errors={})
    pilot = {}
    for member, papers in by_member.items():
        ms2_path = ms2_snapshot_path(Path(roots[member]["ms2_root"]))
        if ms2_path.suffix.lower() != ".zip":
            raise ContractError(f"Candidate scan expects MS2_RESULTS.zip for {member}")
        candidates = []
        with ZipSnapshot(ms2_path) as archive:
            for paper in papers:
                try:
                    raw = json.loads(archive.read_bytes(f"tables/{paper_key(paper)}/bibtex.json").decode("utf-8-sig"))
                    if not isinstance(raw, list):
                        raise ValueError("BibTeX table is not a list")
                except (FileNotFoundError, ValueError, UnicodeError, KeyError) as exc:
                    report["scan_errors"][paper] = str(exc)
                    continue
                count = len(raw)
                if count == 0:
                    continue
                titles = sum(bool(row.get("title")) for row in raw if isinstance(row, dict))
                authors = sum(bool(row.get("authors")) for row in raw if isinstance(row, dict))
                rank = (titles == count, authors == count, titles / count, authors / count,
                        -count, hashlib.sha256(f"{seed}:{paper}".encode()).hexdigest())
                candidates.append((rank, dict(arxiv_id=paper, bibtex_count=count,
                                              titles_present=titles, authors_present=authors)))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) < pilot_per_member:
            raise ContractError(f"Only {len(candidates)} nonempty candidates for {member}")
        top = [row for _, row in candidates[:pilot_per_member]]
        pilot[member] = [row["arxiv_id"] for row in top]
        report["members"][member] = dict(scanned=len(papers), nonempty=len(candidates),
                                         pilot=len(top), pilot_metadata=top)
        print(f"{member}: scanned {len(papers)}, nonempty {len(candidates)}, pilot {len(top)}")

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "candidate_scan.json", report)
    atomic_json(output / "pilot_selection.json", dict(task="T3-05", seed=seed,
                 selection_method=report["selection_method"], selected=pilot,
                 reserves={member: [] for member in pilot}, pilot_only=True))
    return report
