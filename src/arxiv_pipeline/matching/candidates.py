"""TV2 candidate pair generation module for Milestone 3 Reference Matching.

Generates Cartesian m x n candidate pairs within individual publications.
Enforces the invariant that reference matching is strictly within-publication.
"""

from __future__ import annotations

from typing import Iterator

from arxiv_pipeline.matching.contracts import (
    BibtexInput,
    ContractError,
    PairKey,
    PublicationBundle,
    QueryKey,
    ReferenceInput,
)


def generate_candidate_pairs(
    bundle: PublicationBundle,
) -> list[tuple[BibtexInput, ReferenceInput]]:
    """Generate all m x n candidate pairs for a single publication bundle.

    Invariant:
    - Only pairs belonging to the exact same publication (arxiv_id) are generated.
    - If bundle.bibtex or bundle.references is empty, returns an empty list without raising.
    """
    if not isinstance(bundle, PublicationBundle):
        raise ContractError("bundle must be an instance of PublicationBundle")

    paper = bundle.arxiv_id
    pairs: list[tuple[BibtexInput, ReferenceInput]] = []

    for b in bundle.bibtex:
        if b.arxiv_id != paper:
            raise ContractError(f"BibTeX entry belongs to different publication: {b.arxiv_id} != {paper}")
        for r in bundle.references:
            if r.arxiv_id != paper:
                raise ContractError(f"Reference belongs to different publication: {r.arxiv_id} != {paper}")
            pairs.append((b, r))

    return pairs


def build_pairs(
    bibtex: list[BibtexInput] | PublicationBundle,
    references: list[ReferenceInput] | None = None,
) -> list[tuple[BibtexInput, ReferenceInput]]:
    """Convenience adapter supporting both bundle and (bibtex, references) signatures."""
    if isinstance(bibtex, PublicationBundle):
        return generate_candidate_pairs(bibtex)
    if references is None:
        raise ContractError("references must be provided when bibtex is a list")
    pairs: list[tuple[BibtexInput, ReferenceInput]] = []
    for b in bibtex:
        for r in references:
            if b.arxiv_id != r.arxiv_id:
                raise ContractError(
                    f"Cross-publication pairing detected: {b.arxiv_id} != {r.arxiv_id}"
                )
            pairs.append((b, r))
    return pairs


def iterate_candidate_chunks(
    bundle: PublicationBundle,
    chunk_size: int = 1000,
) -> Iterator[list[tuple[BibtexInput, ReferenceInput]]]:
    """Iterate over candidate pairs in fixed-size chunks for large candidate spaces."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    current_chunk: list[tuple[BibtexInput, ReferenceInput]] = []
    paper = bundle.arxiv_id

    for b in bundle.bibtex:
        for r in bundle.references:
            if b.arxiv_id != paper or r.arxiv_id != paper:
                raise ContractError("Cross-publication pairing detected")
            current_chunk.append((b, r))
            if len(current_chunk) >= chunk_size:
                yield current_chunk
                current_chunk = []
    if current_chunk:
        yield current_chunk


def get_query_keys(bundle: PublicationBundle) -> list[QueryKey]:
    """Extract all distinct QueryKeys (arxiv_id, citation_key) from a bundle."""
    return [(bundle.arxiv_id, b.citation_key) for b in bundle.bibtex]


def get_pair_keys(bundle: PublicationBundle) -> list[PairKey]:
    """Extract all PairKeys (arxiv_id, citation_key, ref_arxiv_id) for a bundle."""
    return [
        (bundle.arxiv_id, b.citation_key, r.ref_arxiv_id)
        for b in bundle.bibtex
        for r in bundle.references
    ]
