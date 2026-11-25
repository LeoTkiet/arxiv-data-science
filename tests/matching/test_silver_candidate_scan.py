"""Candidate scan ranks a diagnostic pilot without inventing labels."""
import json
import zipfile

from arxiv_pipeline.common.utils import atomic_json
from arxiv_pipeline.matching.silver_candidate_scan import scan_candidates


def test_candidate_scan_prefers_nonempty_complete_bibtex(tmp_path):
    starts = ["2508.12872", "2506.15788", "2411.08799", "2410.18025", "2410.03025"]
    ranges = tmp_path / "ranges.json"
    atomic_json(ranges, {"ranges": [{"range": f"{start}:{start}"} for start in starts]})
    roots = {}
    for index, paper in enumerate(starts, 1):
        member = f"tv{index}"
        archive_path = tmp_path / f"{member}.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(f"{member}/state/parsing/{paper}.json", "{}")
            archive.writestr(f"{member}/tables/{paper}/bibtex.json", json.dumps([
                {"title": "Complete", "authors": ["A"]},
            ]))
        roots[member] = {"ms2_root": str(archive_path), "ms1_root": "unused"}
    population = tmp_path / "population.json"
    atomic_json(population, {"publication_ids": starts, "excluded_manual_ids": []})
    selection = tmp_path / "selection.json"
    atomic_json(selection, {"task": "T3-05", "selected": {}})
    roots_path = tmp_path / "roots.json"
    atomic_json(roots_path, roots)
    output = tmp_path / "scan"
    report = scan_candidates(population, selection, roots_path, ranges, output,
                             pilot_per_member=1)
    assert all(row["pilot"] == 1 for row in report["members"].values())
    pilot = json.loads((output / "pilot_selection.json").read_text())
    assert [pilot["selected"][f"tv{i}"][0] for i in range(1, 6)] == starts
    assert pilot["pilot_only"] is True
