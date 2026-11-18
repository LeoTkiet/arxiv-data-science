"""Unit tests for TV2 feature engineering module."""

import pytest
from arxiv_pipeline.matching.contracts import (
    FeatureSchema,
    PairFeature,
    publication_from_rows,
    validate_features,
)
from arxiv_pipeline.matching.features import (
    build_feature_batch,
    compute_pair_feature,
    extract_pair_features,
    get_feature_schema,
)
from fixtures.matching.examples import publications


def test_feature_schema_validity():
    schema = get_feature_schema()
    assert isinstance(schema, FeatureSchema)
    assert len(schema.features) > 10
    names = [f.name for f in schema.features]
    assert len(names) == len(set(names))
    for f in schema.features:
        assert f.dtype in {"float64", "int64", "bool"}
        assert len(f.source_fields) > 0
        assert f.rationale.strip() != ""
        assert f.missing_policy.strip() != ""


def test_build_feature_batch_on_fixtures():
    pubs = publications()
    bundles = [
        publication_from_rows(p["arxiv_id"], p["bibtex"], p["references"], readiness="ready")
        for p in pubs
    ]
    schema = get_feature_schema()
    batch = build_feature_batch(bundles, schema)

    # Validate against official contract validator
    report = validate_features(batch, schema)
    assert report.valid, f"Validation errors: {report.errors}"
    assert report.counts["pairs"] == len(batch.rows)


def test_exact_match_features():
    pubs = publications()
    exact_pub = next(p for p in pubs if p["case"] == "exact")
    bundle = publication_from_rows(exact_pub["arxiv_id"], exact_pub["bibtex"], exact_pub["references"], readiness="ready")
    b = bundle.bibtex[0]
    r = bundle.references[0]

    feat = extract_pair_features(b, r, citing_year=2023)
    assert feat["title_exact_match"] == 1
    assert feat["title_jaccard_word"] == 1.0
    assert feat["title_levenshtein_ratio"] == 1.0
    assert feat["title_len_diff_ratio"] == 0.0
    assert feat["first_author_match"] == 1
    assert feat["author_jaccard_last"] == 1.0
    assert feat["year_exact_match"] == 1
    assert feat["year_diff_abs"] == 0.0
    assert feat["year_is_chronological"] == 1


def test_unmatched_and_missing_features():
    pubs = publications()
    unmatched_pub = next(p for p in pubs if p["case"] == "unmatched")
    bundle = publication_from_rows(unmatched_pub["arxiv_id"], unmatched_pub["bibtex"], unmatched_pub["references"], readiness="ready")
    b = bundle.bibtex[0]
    r = bundle.references[0]  # Reference with null title

    feat = extract_pair_features(b, r, citing_year=2023)
    assert feat["title_exact_match"] == 0
    assert feat["title_jaccard_word"] == 0.0
    assert feat["title_levenshtein_ratio"] == 0.0
    assert feat["title_len_diff_ratio"] == 1.0
    assert feat["year_exact_match"] == 1  # Both happen to be 2020 in synthetic fixture
    assert feat["year_diff_abs"] == 0.0

    # Test with explicitly missing year and title
    class MockBib:
        arxiv_id = "2310.99999"
        citation_key = "MissingKey"
        title = ""
        authors = []
        year = None
        doi = None
        url = None
        journal = None

    class MockRef:
        arxiv_id = "2310.99999"
        ref_arxiv_id = "2001.99999"
        ref_title = ""
        ref_authors = []
        ref_submission_date = None

    missing_feat = extract_pair_features(MockBib(), MockRef())
    assert missing_feat["is_bib_year_missing"] == 1
    assert missing_feat["is_ref_year_missing"] == 1
    assert missing_feat["year_exact_match"] == 0
    assert missing_feat["year_diff_abs"] == 99.0
    assert missing_feat["title_exact_match"] == 0


def test_levenshtein_ratio_determinism_and_reproducibility():
    from arxiv_pipeline.matching.features import _levenshtein_ratio, _levenshtein_ratio_pure

    # Reviewer case: 'tide' vs 'diet' must equal 0.5 deterministically
    assert _levenshtein_ratio("tide", "diet") == 0.5
    assert _levenshtein_ratio_pure("tide", "diet") == 0.5

    # Check identical values between pure and optional accelerated implementations
    test_pairs = [
        ("", ""),
        ("abc", ""),
        ("", "xyz"),
        ("identical string", "identical string"),
        ("quantum computing", "quantum communication"),
        ("neural network", "neural networks"),
        ("attention is all you need", "attention mechanisms"),
    ]
    for s1, s2 in test_pairs:
        pure_val = _levenshtein_ratio_pure(s1, s2)
        actual_val = _levenshtein_ratio(s1, s2)
        assert abs(pure_val - actual_val) < 1e-12, f"Discrepancy for {s1} vs {s2}: {pure_val} vs {actual_val}"


def test_build_feature_batch_rejects_incompatible_schema():
    from arxiv_pipeline.matching.contracts import ContractError, FeatureSpec

    pubs = publications()
    bundles = [
        publication_from_rows(p["arxiv_id"], p["bibtex"], p["references"], readiness="ready")
        for p in pubs
    ]

    # Incompatible schema with different columns
    custom_spec = FeatureSpec(
        name="custom_metric",
        dtype="float64",
        nullable=False,
        minimum=0.0,
        maximum=1.0,
        missing_policy="error",
        rationale="Custom metric rationale",
        source_fields=["title"],
    )
    incompatible_schema = FeatureSchema([custom_spec])

    with pytest.raises(ContractError, match="Unsupported feature schema"):
        build_feature_batch(bundles, schema=incompatible_schema)
