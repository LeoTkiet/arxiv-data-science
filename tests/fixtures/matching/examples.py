"""Small, deterministic M1/M2-shaped examples; no external data or network."""

from copy import deepcopy
from pathlib import Path

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key


def _bib(paper, title):
    return dict(arxiv_id=paper, citation_key="RefA", entry_type="article",
                title=title, authors=["Nguyễn An"], year=2020,
                doi=None, journal=None, url=None)


def _ref(paper, target, title):
    row = dict(arxiv_id=paper, ref_arxiv_id=target, ref_title=title,
               ref_authors=["Nguyễn An"], ref_submission_date="2020-01-02",
               ref_s2_id=None)
    row["missing_fields"] = [key for key, value in row.items()
                             if key != "arxiv_id" and value in (None, [])]
    return row


def publications():
    """Return fresh rows so tests/consumers cannot corrupt later examples."""
    return deepcopy([
        dict(case="exact", arxiv_id="2310.12345", readiness="ready",
             bibtex=[_bib("2310.12345v2", "Synthetic exact title")],
             references=[_ref("2310.12345", "2001.00001v1", "Synthetic exact title")]),
        dict(case="ambiguous", arxiv_id="2310.12346", readiness="ready",
             bibtex=[_bib("2310.12346", "Synthetic ambiguous title")],
             references=[_ref("2310.12346", "2001.00002", "Synthetic ambiguous title"),
                         _ref("2310.12346", "2001.00003", "Synthetic ambiguous title")]),
        dict(case="unmatched", arxiv_id="2310.12347", readiness="ready",
             bibtex=[_bib("2310.12347", "Synthetic unrelated work")],
             references=[_ref("2310.12347", "2001.00004", None),
                         _ref("2310.12347", None, "Synthetic non-arXiv reference")]),
        dict(case="empty_references", arxiv_id="hep-th/9901001", readiness="ready",
             bibtex=[_bib("hep-th/9901001v1", "")], references=[]),
    ])


def labels():
    return [
        dict(arxiv_id="2310.12345", citation_key="RefA", groundtruth_arxiv_id="2001.00001",
             decision="matched", label_source="manual", annotator="synthetic-tv1",
             reviewer="synthetic-tv5", review_status="accepted"),
        dict(arxiv_id="2310.12346", citation_key="RefA", groundtruth_arxiv_id=None,
             decision="uncertain", label_source="manual", annotator="synthetic-tv2"),
        dict(arxiv_id="2310.12347", citation_key="RefA", groundtruth_arxiv_id=None,
             decision="unmatched", label_source="heuristic", confidence=0.95,
             heuristic_version="synthetic-rule-1", review_status="accepted"),
        # The legacy query deliberately has no label: it must not become unmatched.
    ]


def partition_rows():
    """25 manual + 12 auto publications for a five-member split smoke test."""
    rows = []
    for pool, count, prefix in (("manual", 25, "2311"), ("auto", 12, "2312")):
        for index in range(1, count + 1):
            partition = "valid" if index <= 5 else "test" if index <= 10 else "train"
            rows.append(dict(arxiv_id=f"{prefix}.{index:05d}", partition=partition, pool=pool))
    return rows


def write_compatibility_fixture(root):
    """Write a new synthetic snapshot layout for downstream loader development.

    These checkpoints are mock evidence only, explicitly marked by the root
    manifest. Real snapshot/readiness validation is not exercised by this writer.
    Never overwrite an existing directory or place mock data inside a real run.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    ms1, ms2 = root / "ms1", root / "ms2"
    stamp = "2026-11-11T00:00:00+00:00"
    for item in publications():
        paper, key = item["arxiv_id"], paper_key(item["arxiv_id"])
        refs_rel = f"references/{key}.json"
        atomic_json(ms1 / refs_rel, item["references"])
        refs = StageResult("references", paper, None,
                           "success" if item["references"] else "no_data", stamp,
                           artifacts={"references": refs_rel})
        atomic_json(ms1 / f"state/stages/references/{key}/paper.json", refs.to_dict())
        # Real dedup output uses the canonical citing ID, unlike the adversarial
        # in-memory versioned-ID example above.
        bibtex = [dict(row, arxiv_id=paper) for row in item["bibtex"]]
        bib_rel = f"tables/{key}/bibtex.json"
        atomic_json(ms2 / bib_rel, bibtex)
        atomic_json(ms2 / f"tables/{key}/parsing_manifest.json", dict(
            schema_version=PARSING_SCHEMA_VERSION, arxiv_id=paper,
            input_snapshot_id="synthetic-ms1", expected_versions=[1], complete=True,
            versions=[dict(version=1, status="success", reason=None, artifacts={})],
            tables={"bibtex": bib_rel}, counts=dict(elements_before=0, elements_after=0,
                bibtex_before=len(bibtex), bibtex_after=len(bibtex))))
    atomic_json(root / "fixture_manifest.json", dict(synthetic=True, seed=42,
        note="Compatibility fixtures only; not corpus, quota evidence or ML results",
        publications=[dict(arxiv_id=p["arxiv_id"], case=p["case"]) for p in publications()]))
    atomic_json(root / "labels.json", labels())
    atomic_json(root / "partition_rows.json", partition_rows())
    return {"ms1": ms1, "ms2": ms2}
