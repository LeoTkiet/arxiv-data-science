"""Source recovery requires explicit evidence and preserves unresolved queries."""
from arxiv_pipeline.matching.heuristics import normalize_title
from arxiv_pipeline.matching.silver_recovery import recover_query


def test_subscripts_preserve_values():
    assert normalize_title(r"$G_0W_0$ fitting") == normalize_title("G0W0 fitting")
    assert normalize_title(r"$D_{6}$ symmetry") == normalize_title("D6 symmetry")
    assert normalize_title(r"$D_6$ symmetry") != normalize_title(r"$D_4$ symmetry")


def test_recovery_keeps_query_identity_and_requires_unique_evidence():
    query = dict(arxiv_id="2508.12872", citation_key="example", title="", authors=[],
                 entry_type="article", year=None, doi=None, url=None, journal=None)
    sources = [dict(raw_text=r"\bibitem{example} \bibinfo{title}{A precise title} arXiv:2001.12345")]
    recovered, changes = recover_query(query, sources)
    assert recovered["citation_key"] == query["citation_key"]
    assert changes == dict(title="A precise title", url="https://arxiv.org/abs/2001.12345")
    assert query["title"] == ""
    _, conflicting = recover_query(query, sources + [dict(
        raw_text=r"\bibitem{example} \bibinfo{title}{Another precise title} arXiv:2002.12345")])
    assert conflicting == {}
    assert recover_query(query, [dict(raw_text=r"\bibitem{example} unexplained note")])[1] == {}


def test_recovery_runs_from_hashed_zip_to_full_coverage(tmp_path):
    import hashlib
    import json
    import zipfile
    from arxiv_pipeline.common.utils import atomic_json
    from arxiv_pipeline.matching.silver_first_pass import run_first_pass
    from arxiv_pipeline.matching.silver_recovery import run_recovery

    paper = "2508.12872"
    query = dict(arxiv_id=paper, citation_key="example", title="", authors=[],
                 entry_type="article", year=None, doi=None, url=None, journal=None)
    ref = dict(arxiv_id=paper, ref_arxiv_id="2001.12345", ref_title="A precise title", ref_authors=[])
    manifest_rel = f"tables/{paper}/parsing_manifest.json"
    side_rel = f"parsed/{paper}/v1/raw_bibtex.provenance.json"
    checkpoint = f"state/stages/bibliography/{paper}/1.json"
    contents = {manifest_rel: json.dumps(dict(expected_versions=[1])).encode(),
                side_rel: json.dumps(dict(arxiv_id=paper, version=1, schema_version="2.0",
                    items=[dict(citation_key="example", raw_text=r"\bibitem{example} arXiv:2001.12345")])).encode()}
    archive_path = tmp_path / "ms2.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(f"state/parsing/{paper}.json", "{}")
        for path, data in contents.items():
            archive.writestr(path, data)
        archive.writestr(checkpoint, json.dumps(dict(paper_id=paper, version=1,
            schema_version="2.0", status="success", artifacts=dict(provenance=side_rel))))
        archive.writestr(checkpoint.replace(".json", ".dependencies.json"), json.dumps(
            dict(artifacts={side_rel: hashlib.sha256(contents[side_rel]).hexdigest()})))
    prepared = tmp_path / "prepared/prepared_manifest.json"
    bundle_path = prepared.parent / "bundle.json"
    atomic_json(bundle_path, dict(arxiv_id=paper, bibtex=[query], references=[ref], readiness="ready"))
    atomic_json(prepared, dict(task="T3-05", total=1, ready=1, not_ready=0, publications=[dict(
        arxiv_id=paper, member="tv1", status="ready", bundle="bundle.json",
        sha256=hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        provenance=dict(files={"ms2/" + manifest_rel: hashlib.sha256(contents[manifest_rel]).hexdigest()}))]))
    population = tmp_path / "population.json"
    atomic_json(population, dict(publication_ids=[paper, "2508.12873"], population_count=2,
                                excluded_manual_ids=["2508.12873"]))
    roots = tmp_path / "roots.json"
    atomic_json(roots, dict(tv1=dict(ms2_root=str(archive_path))))
    baseline = tmp_path / "baseline"
    run_first_pass(prepared, population, baseline)
    result = run_recovery(prepared, population, roots, baseline, tmp_path / "recovery")
    assert result["baseline_full"] == 0
    assert result["recovered_full"] == 1
    assert result["source_errors"] == 0
    assert result["ready_for_handoff"] is False
