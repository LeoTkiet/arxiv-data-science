"""TV2 feature engineering module for Milestone 3 Reference Matching.

Extracts numeric/boolean feature matrices from candidate (BibTeX, Reference) pairs.
Strictly adheres to FeatureSchema (v3.0) and validates with contracts.validate_features.
"""

from __future__ import annotations

from collections import Counter
import math
from typing import Any

from arxiv_pipeline.matching.cleaning import (
    canonical_doi,
    extract_arxiv_id,
    normalize_bibtex,
    normalize_reference,
    normalize_title,
    parse_authors,
    parse_year,
)
from arxiv_pipeline.matching.contracts import (
    BibtexInput,
    ContractError,
    FeatureBatch,
    FeatureSchema,
    FeatureSpec,
    PairFeature,
    PublicationBundle,
    ReferenceInput,
    validate_features,
)


def _levenshtein_ratio_pure(s1: str, s2: str) -> float:
    """Compute exact, deterministic Levenshtein similarity ratio (substitution cost = 2).

    Formula: (len(s1) + len(s2) - indel_distance) / (len(s1) + len(s2)).
    Guarantees reproducible, bit-consistent results across all runtime environments.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    m, n = len(s1), len(s2)
    if m < n:
        s1, s2 = s2, s1
        m, n = n, m

    prev = list(range(n + 1))
    for i, c1 in enumerate(s1, 1):
        curr = [i] * (n + 1)
        for j, c2 in enumerate(s2, 1):
            cost = 0 if c1 == c2 else 2
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    dist = prev[n]
    return float((m + n - dist) / (m + n))


# Optional rapidfuzz for SIMD acceleration, with mathematically identical pure-Python fallback
try:
    import rapidfuzz.fuzz as rfuzz

    def _levenshtein_ratio(s1: str, s2: str) -> float:
        if not s1 and not s2:
            return 1.0
        if not s1 or not s2:
            return 0.0
        # rapidfuzz.fuzz.ratio uses the exact same indel distance formula: (m + n - dist) / (m + n)
        return float(rfuzz.ratio(s1, s2) / 100.0)

except ImportError:
    _levenshtein_ratio = _levenshtein_ratio_pure


def _char_3grams(text: str) -> Counter[str]:
    """Extract character 3-grams from normalized text."""
    if len(text) < 3:
        return Counter([text] if text else [])
    return Counter(text[i : i + 3] for i in range(len(text) - 2))


def _cosine_counter(c1: Counter[str], c2: Counter[str]) -> float:
    """Compute cosine similarity between two frequency Counters."""
    if not c1 or not c2:
        return 0.0
    common = set(c1) & set(c2)
    if not common:
        return 0.0
    dot = sum(c1[k] * c2[k] for k in common)
    norm1 = math.sqrt(sum(v * v for v in c1.values()))
    norm2 = math.sqrt(sum(v * v for v in c2.values()))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    val = dot / (norm1 * norm2)
    return max(0.0, min(1.0, float(val)))


def _jaccard_tokens(tokens1: set[str], tokens2: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not tokens1 and not tokens2:
        return 0.0
    union = tokens1 | tokens2
    if not union:
        return 0.0
    return float(len(tokens1 & tokens2) / len(union))


def get_feature_schema() -> FeatureSchema:
    """Return the official FeatureSchema for Milestone 3 Reference Matching."""
    specs = [
        FeatureSpec(
            name="title_exact_match",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if either title is empty.",
            rationale="Exact match indicator between cleaned BibTeX title and candidate reference title.",
            source_fields=["title", "ref_title"],
        ),
        FeatureSpec(
            name="title_jaccard_word",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=1.0,
            missing_policy="Defaults to 0.0 if either title has no tokens.",
            rationale="Token-level Jaccard similarity of normalized words in titles.",
            source_fields=["title", "ref_title"],
        ),
        FeatureSpec(
            name="title_levenshtein_ratio",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=1.0,
            missing_policy="Defaults to 0.0 if either title is empty.",
            rationale="Character-level Levenshtein similarity ratio for typo resilience.",
            source_fields=["title", "ref_title"],
        ),
        FeatureSpec(
            name="title_char_ngram_cosine",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=1.0,
            missing_policy="Defaults to 0.0 if either title has fewer than 3 characters.",
            rationale="Cosine similarity of character 3-gram frequencies.",
            source_fields=["title", "ref_title"],
        ),
        FeatureSpec(
            name="title_len_diff_ratio",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=1.0,
            missing_policy="Defaults to 1.0 if either title is empty.",
            rationale="Normalized relative length difference between titles.",
            source_fields=["title", "ref_title"],
        ),
        FeatureSpec(
            name="author_jaccard_last",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=1.0,
            missing_policy="Defaults to 0.0 if author lists cannot be parsed.",
            rationale="Jaccard similarity between sets of authors' standardized last names.",
            source_fields=["authors", "ref_authors"],
        ),
        FeatureSpec(
            name="first_author_match",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if first author is missing on either side.",
            rationale="Indicator if primary/first author last name matches exactly.",
            source_fields=["authors", "ref_authors"],
        ),
        FeatureSpec(
            name="author_count_diff",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=100,
            missing_policy="Defaults to 0 if both lists are empty.",
            rationale="Absolute difference in parsed author counts (capped at 100).",
            source_fields=["authors", "ref_authors"],
        ),
        FeatureSpec(
            name="doi_exact_match",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if DOI is missing on either side.",
            rationale="High-precision exact match of canonical DOIs.",
            source_fields=["doi"],
        ),
        FeatureSpec(
            name="has_doi_both",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if either entity lacks a DOI.",
            rationale="Indicator whether both BibTeX and candidate possess DOI metadata.",
            source_fields=["doi"],
        ),
        FeatureSpec(
            name="arxiv_id_match",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if no arXiv ID can be extracted from BibTeX.",
            rationale="Direct match between BibTeX-extracted arXiv ID and candidate ref_arxiv_id.",
            source_fields=["url", "journal", "citation_key", "ref_arxiv_id"],
        ),
        FeatureSpec(
            name="year_exact_match",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 0 if year is missing on either side.",
            rationale="Indicator if publication years are identical.",
            source_fields=["year", "ref_submission_date"],
        ),
        FeatureSpec(
            name="year_diff_abs",
            dtype="float64",
            nullable=False,
            minimum=0.0,
            maximum=100.0,
            missing_policy="Imputed to 99.0 if year is missing on either side.",
            rationale="Absolute difference in publication years (penalizes distant citations).",
            source_fields=["year", "ref_submission_date"],
        ),
        FeatureSpec(
            name="year_is_chronological",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Defaults to 1 if citing year cannot be established.",
            rationale="Physical validity check: cited reference year <= citing publication year.",
            source_fields=["year", "ref_submission_date", "arxiv_id"],
        ),
        FeatureSpec(
            name="is_bib_year_missing",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Non-null indicator flag (0 or 1).",
            rationale="Missingness indicator flag for BibTeX publication year.",
            source_fields=["year"],
        ),
        FeatureSpec(
            name="is_ref_year_missing",
            dtype="int64",
            nullable=False,
            minimum=0,
            maximum=1,
            missing_policy="Non-null indicator flag (0 or 1).",
            rationale="Missingness indicator flag for Reference publication year.",
            source_fields=["ref_submission_date"],
        ),
    ]
    return FeatureSchema(specs)


def extract_pair_features(
    bibtex: BibtexInput,
    reference: ReferenceInput,
    *,
    citing_year: int | None = None,
) -> dict[str, float | int | bool | None]:
    """Extract numeric/bool feature values for a single (BibTeX, Reference) pair."""
    # 1. Cleaned representations
    b_title = normalize_title(bibtex.title)
    r_title = normalize_title(reference.ref_title)

    b_authors = parse_authors(bibtex.authors)
    r_authors = parse_authors(reference.ref_authors)

    b_year = parse_year(bibtex.year)
    r_year = parse_year(reference.ref_submission_date)

    b_doi = canonical_doi(bibtex.doi)
    # ReferenceInput does not carry ref_doi in contract 3.0
    r_doi = None

    # Extracted arXiv ID from BibTeX
    extracted_arxiv = (
        extract_arxiv_id(bibtex.url)
        or extract_arxiv_id(bibtex.journal)
        or extract_arxiv_id(bibtex.citation_key)
    )

    # 2. Title Features
    if b_title and r_title:
        title_exact = 1 if b_title == r_title else 0
        b_words = set(b_title.split())
        r_words = set(r_title.split())
        title_jaccard = _jaccard_tokens(b_words, r_words)
        title_lev = _levenshtein_ratio(b_title, r_title)
        title_cosine = _cosine_counter(_char_3grams(b_title), _char_3grams(r_title))
        max_len = max(len(b_title), len(r_title), 1)
        title_len_diff = float(abs(len(b_title) - len(r_title)) / max_len)
    else:
        title_exact = 0
        title_jaccard = 0.0
        title_lev = 0.0
        title_cosine = 0.0
        title_len_diff = 1.0

    # 3. Author Features
    b_lasts = {a["last_name"] for a in b_authors if a.get("last_name")}
    r_lasts = {a["last_name"] for a in r_authors if a.get("last_name")}
    author_jaccard = _jaccard_tokens(b_lasts, r_lasts)

    first_author_match = 0
    if b_authors and r_authors:
        if b_authors[0]["last_name"] == r_authors[0]["last_name"]:
            first_author_match = 1

    author_count_diff = min(abs(len(b_authors) - len(r_authors)), 100)

    # 4. Identifier Features
    doi_match = 1 if (b_doi and r_doi and b_doi == r_doi) else 0
    has_doi_both = 1 if (b_doi and r_doi) else 0

    arxiv_id_match = 0
    if extracted_arxiv and reference.ref_arxiv_id:
        if extracted_arxiv == reference.ref_arxiv_id:
            arxiv_id_match = 1

    # 5. Temporal / Year Features
    is_bib_year_missing = 1 if b_year is None else 0
    is_ref_year_missing = 1 if r_year is None else 0

    if b_year is not None and r_year is not None:
        year_exact = 1 if b_year == r_year else 0
        year_diff = min(float(abs(b_year - r_year)), 100.0)
    else:
        year_exact = 0
        year_diff = 99.0

    # Physical validity: citing publication year >= cited reference year
    year_is_chronological = 1
    if citing_year is not None and r_year is not None:
        if r_year > citing_year:
            year_is_chronological = 0
    elif b_year is not None and r_year is not None:
        if r_year > b_year:
            year_is_chronological = 0

    return {
        "title_exact_match": int(title_exact),
        "title_jaccard_word": float(title_jaccard),
        "title_levenshtein_ratio": float(title_lev),
        "title_char_ngram_cosine": float(title_cosine),
        "title_len_diff_ratio": float(title_len_diff),
        "author_jaccard_last": float(author_jaccard),
        "first_author_match": int(first_author_match),
        "author_count_diff": int(author_count_diff),
        "doi_exact_match": int(doi_match),
        "has_doi_both": int(has_doi_both),
        "arxiv_id_match": int(arxiv_id_match),
        "year_exact_match": int(year_exact),
        "year_diff_abs": float(year_diff),
        "year_is_chronological": int(year_is_chronological),
        "is_bib_year_missing": int(is_bib_year_missing),
        "is_ref_year_missing": int(is_ref_year_missing),
    }


def compute_pair_feature(
    bibtex: BibtexInput,
    reference: ReferenceInput,
    *,
    citing_year: int | None = None,
) -> PairFeature:
    """Build a typed PairFeature for a single candidate pair."""
    if bibtex.arxiv_id != reference.arxiv_id:
        raise ContractError("Cannot extract features for cross-publication pair")
    vals = extract_pair_features(bibtex, reference, citing_year=citing_year)
    return PairFeature(
        arxiv_id=bibtex.arxiv_id,
        citation_key=bibtex.citation_key,
        ref_arxiv_id=reference.ref_arxiv_id,
        values=vals,
    )


def build_feature_batch(
    bundles: list[PublicationBundle] | PublicationBundle,
    schema: FeatureSchema | None = None,
) -> FeatureBatch:
    """Extract all candidate pair features for the given bundles into a validated FeatureBatch.

    Guarantees:
    - Rejects incompatible/unsupported feature schemas.
    - Validates batch against schema using contracts.validate_features before returning.
    """
    if isinstance(bundles, PublicationBundle):
        bundles = [bundles]
    default_schema = get_feature_schema()
    if schema is None:
        schema = default_schema
    elif schema.schema_id != default_schema.schema_id:
        raise ContractError(
            f"Unsupported feature schema: {schema.schema_id}. "
            f"Expected registered schema {default_schema.schema_id}."
        )

    rows: list[PairFeature] = []
    for bundle in bundles:
        paper = bundle.arxiv_id
        # Infer citing year from bundle arXiv ID if possible (e.g. 2310 -> 2023)
        citing_year = None
        if "." in paper:
            prefix = paper.split(".")[0]
            if len(prefix) == 4 and prefix.isdigit():
                yy = int(prefix[:2])
                citing_year = 2000 + yy if yy < 90 else 1900 + yy

        for b in bundle.bibtex:
            for r in bundle.references:
                rows.append(compute_pair_feature(b, r, citing_year=citing_year))

    batch = FeatureBatch(rows=rows, feature_schema_id=schema.schema_id)
    report = validate_features(batch, schema)
    if not report.valid:
        error_msgs = [f"{i.code}: {i.message}" for i in report.errors]
        raise ContractError(f"Generated FeatureBatch failed validation: {'; '.join(error_msgs)}")
    return batch


def transform_pairs(
    pairs: list[tuple[BibtexInput, ReferenceInput]],
    schema: FeatureSchema | None = None,
) -> FeatureBatch:
    """Transform candidate (BibTeX, Reference) pairs directly into a validated FeatureBatch."""
    default_schema = get_feature_schema()
    if schema is None:
        schema = default_schema
    elif schema.schema_id != default_schema.schema_id:
        raise ContractError(
            f"Unsupported feature schema: {schema.schema_id}. "
            f"Expected registered schema {default_schema.schema_id}."
        )

    rows: list[PairFeature] = []
    for b, r in pairs:
        paper = b.arxiv_id
        citing_year = None
        if "." in paper:
            prefix = paper.split(".")[0]
            if len(prefix) == 4 and prefix.isdigit():
                yy = int(prefix[:2])
                citing_year = 2000 + yy if yy < 90 else 1900 + yy
        rows.append(compute_pair_feature(b, r, citing_year=citing_year))

    batch = FeatureBatch(rows=rows, feature_schema_id=schema.schema_id)
    report = validate_features(batch, schema)
    if not report.valid:
        error_msgs = [f"{i.code}: {i.message}" for i in report.errors]
        raise ContractError(f"Generated FeatureBatch failed validation: {'; '.join(error_msgs)}")
    return batch
