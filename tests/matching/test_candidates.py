"""Unit tests for TV2 candidate generation module."""

import pytest
from arxiv_pipeline.matching.candidates import (
    build_pairs,
    generate_candidate_pairs,
    get_pair_keys,
    get_query_keys,
    iterate_candidate_chunks,
)
from arxiv_pipeline.matching.contracts import (
    BibtexInput,
    ContractError,
    PublicationBundle,
    ReferenceInput,
    publication_from_rows,
)
from fixtures.matching.examples import publications


def test_generate_candidate_pairs_within_publication():
    pubs = publications()
    bundle = publication_from_rows(pubs[1]["arxiv_id"], pubs[1]["bibtex"], pubs[1]["references"], readiness="ready")
    pairs = generate_candidate_pairs(bundle)
    assert len(pairs) == len(bundle.bibtex) * len(bundle.references)
    for b, r in pairs:
        assert b.arxiv_id == bundle.arxiv_id
        assert r.arxiv_id == bundle.arxiv_id


def test_empty_candidates_handling():
    pubs = publications()
    # Case with empty references
    empty_ref_pub = next(p for p in pubs if p["case"] == "empty_references")
    bundle = publication_from_rows(empty_ref_pub["arxiv_id"], empty_ref_pub["bibtex"], empty_ref_pub["references"], readiness="ready")
    pairs = generate_candidate_pairs(bundle)
    assert pairs == []


def test_candidate_chunks():
    pubs = publications()
    bundle = publication_from_rows(pubs[1]["arxiv_id"], pubs[1]["bibtex"], pubs[1]["references"], readiness="ready")
    chunks = list(iterate_candidate_chunks(bundle, chunk_size=1))
    assert len(chunks) == 2
    assert len(chunks[0]) == 1
    assert len(chunks[1]) == 1


def test_get_keys():
    pubs = publications()
    bundle = publication_from_rows(pubs[1]["arxiv_id"], pubs[1]["bibtex"], pubs[1]["references"], readiness="ready")
    q_keys = get_query_keys(bundle)
    assert q_keys == [(bundle.arxiv_id, "RefA")]
    p_keys = get_pair_keys(bundle)
    assert len(p_keys) == 2
    assert p_keys[0] == (bundle.arxiv_id, "RefA", "2001.00002")
    assert p_keys[1] == (bundle.arxiv_id, "RefA", "2001.00003")


def test_build_pairs_signatures():
    pubs = publications()
    bundle = publication_from_rows(pubs[1]["arxiv_id"], pubs[1]["bibtex"], pubs[1]["references"], readiness="ready")
    # Bundle signature
    pairs_bundle = build_pairs(bundle)
    assert len(pairs_bundle) == 2
    # Lists signature
    pairs_lists = build_pairs(bundle.bibtex, bundle.references)
    assert pairs_lists == pairs_bundle
