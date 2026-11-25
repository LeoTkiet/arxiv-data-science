"""High-precision heuristic auto-labeling for Reference Matching (Milestone 3 · TV3).

Implements `auto_label(bundles, policy)` following `docs/MS3_CONTRACTS.md` and
`docs/matching/annotation_guideline.md`. Conservative by design: ambiguous cases
abstain with `decision="uncertain"` instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata
from typing import Iterable

from arxiv_pipeline.matching.contracts import (
    BibtexInput,
    ContractError,
    LabelRecord,
    PublicationBundle,
    ReferenceInput,
    canonical_id,
)

# arXiv ID patterns: modern YYMM.NNNNN[vN] and legacy archive/YYMMNNN[vN]
_NEW_ARXIV_ID_RE = re.compile(r"(?<![0-9])(\d{4}\.\d{4,5}(?:v\d+)?)(?![0-9])", re.I)
_OLD_ARXIV_ID_RE = re.compile(
    r"(?<![a-z0-9])([a-z-]+(?:\.[a-z-]+)?/\d{7}(?:v\d+)?)(?![0-9])",
    re.I,
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_AUTHOR_NOISE = {"others", "et", "al", "collaboration", "collaborators", "group", "consortium"}
_LATEX_WRAPPER_RE = re.compile(r"\\[a-zA-Z]+\s*\{([^{}]*)\}")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")


@dataclass(frozen=True)
class HeuristicPolicy:
    """Configurable thresholds and policy for high-precision heuristic labeling."""

    heuristic_version: str = "v1.2.0"
    exact_title_threshold: float = 0.96
    high_title_threshold: float = 0.84
    no_match_title_threshold: float = 0.25
    min_title_chars_for_exact: int = 8
    min_margin_over_second: float = 0.10
    max_year_diff: int = 3
    accept_clear_decisions: bool = False


def _strip_accents(text: str) -> str:
    """Normalize Unicode accents to ASCII base characters."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(title: str | None) -> str:
    """Normalize a paper title for deterministic comparison."""
    if not title:
        return ""
    cleaned = title
    for _ in range(3):
        updated = _LATEX_WRAPPER_RE.sub(r" \1 ", cleaned)
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = cleaned.replace("$", " ").replace("{", " ").replace("}", " ")
    cleaned = _strip_accents(cleaned).lower()
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def extract_arxiv_ids(text: str | None) -> list[str]:
    """Extract canonical arXiv IDs from a DOI, URL, or journal string."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for pattern in (_NEW_ARXIV_ID_RE, _OLD_ARXIV_ID_RE):
        for match in pattern.finditer(text):
            try:
                cid = canonical_id(match.group(1).lower())
            except ContractError:
                continue
            if cid not in seen:
                seen.add(cid)
                found.append(cid)
    return found


def extract_author_last_names(authors: Iterable[str] | None) -> set[str]:
    """Extract normalized author surnames from either 'First Last' or 'Last, First'."""
    if not authors:
        return set()
    surnames: set[str] = set()
    for raw in authors:
        if not raw or not isinstance(raw, str):
            continue
        author = _strip_accents(raw).replace("{", "").replace("}", "").strip().lower()
        if not author:
            continue
        tokens_all = _NON_ALNUM_RE.sub(" ", author).split()
        if not tokens_all or any(token in _AUTHOR_NOISE for token in tokens_all):
            continue
        if "," in author:
            surname_part = author.split(",", 1)[0].strip()
            tokens = _NON_ALNUM_RE.sub(" ", surname_part).split()
            if tokens and tokens[-1] not in _AUTHOR_NOISE:
                surnames.add(tokens[-1])
        else:
            tokens = _NON_ALNUM_RE.sub(" ", author).split()
            if tokens and tokens[-1] not in _AUTHOR_NOISE:
                surnames.add(tokens[-1])
    return surnames


def extract_submission_year(submission_date: str | None) -> int | None:
    """Parse leading 4-digit year from a reference submission date."""
    if not submission_date:
        return None
    match = _YEAR_RE.search(submission_date)
    return int(match.group(1)) if match else None


def title_similarity(norm_a: str, norm_b: str) -> float:
    """Compute a conservative similarity score in [0.0, 1.0] for two normalized titles."""
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    seq_ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    return 0.5 * jaccard + 0.5 * seq_ratio


def _has_year_conflict(bib_year: int | None, ref_date: str | None, max_diff: int) -> bool:
    if bib_year is None or not 1900 <= bib_year <= 2100:
        return False
    ref_year = extract_submission_year(ref_date)
    if ref_year is None:
        return False
    return abs(bib_year - ref_year) > max_diff


def _make_record(
    bib: BibtexInput,
    *,
    groundtruth_arxiv_id: str | None,
    decision: str,
    confidence: float,
    policy: HeuristicPolicy,
    notes: str,
) -> LabelRecord:
    review_status = (
        "accepted"
        if policy.accept_clear_decisions and decision != "uncertain"
        else "pending"
    )
    return LabelRecord(
        arxiv_id=bib.arxiv_id,
        citation_key=bib.citation_key,
        groundtruth_arxiv_id=groundtruth_arxiv_id,
        decision=decision,
        label_source="heuristic",
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        heuristic_version=policy.heuristic_version,
        review_status=review_status,
        notes=notes,
    )


def _label_single_query(
    bib: BibtexInput,
    references: list[ReferenceInput],
    policy: HeuristicPolicy,
) -> LabelRecord:
    if not references:
        return _make_record(
            bib,
            groundtruth_arxiv_id=None,
            decision="unmatched",
            confidence=0.95,
            policy=policy,
            notes="rule=empty_references",
        )

    bib_title_norm = normalize_title(bib.title)
    bib_surnames = extract_author_last_names(bib.authors)

    # Rule 1: Explicit arXiv ID in doi / url / journal
    explicit_ids: set[str] = set()
    for field in (bib.doi, bib.url, bib.journal):
        explicit_ids.update(extract_arxiv_ids(field))

    ref_by_id = {canonical_id(r.ref_arxiv_id): r for r in references}
    id_matches = [ref_by_id[cid] for cid in explicit_ids if cid in ref_by_id]
    if len(id_matches) == 1:
        matched_ref = id_matches[0]
        ref_title_norm = normalize_title(matched_ref.ref_title)
        if bib_title_norm and ref_title_norm and title_similarity(bib_title_norm, ref_title_norm) < policy.no_match_title_threshold:
            return _make_record(
                bib,
                groundtruth_arxiv_id=None,
                decision="uncertain",
                confidence=0.50,
                policy=policy,
                notes="rule=arxiv_id_title_conflict",
            )
        return _make_record(
            bib,
            groundtruth_arxiv_id=canonical_id(matched_ref.ref_arxiv_id),
            decision="matched",
            confidence=0.99,
            policy=policy,
            notes="rule=explicit_arxiv_id",
        )
    if len(id_matches) > 1:
        return _make_record(
            bib,
            groundtruth_arxiv_id=None,
            decision="uncertain",
            confidence=0.40,
            policy=policy,
            notes="rule=ambiguous_arxiv_id",
        )

    # Score all candidates on title, author, and year consistency
    scored: list[tuple[float, int, bool, ReferenceInput, str]] = []
    any_missing_title = False
    max_raw_sim = 0.0
    max_author_overlap = 0

    for ref in references:
        ref_title_norm = normalize_title(ref.ref_title)
        if not ref_title_norm:
            any_missing_title = True
        sim = title_similarity(bib_title_norm, ref_title_norm)
        ref_surnames = extract_author_last_names(ref.ref_authors)
        overlap = len(bib_surnames & ref_surnames) if (bib_surnames and ref_surnames) else 0
        year_conflict = _has_year_conflict(bib.year, ref.ref_submission_date, policy.max_year_diff)
        max_raw_sim = max(max_raw_sim, sim)
        max_author_overlap = max(max_author_overlap, overlap)
        scored.append((sim, overlap, year_conflict, ref, ref_title_norm))

    scored.sort(key=lambda item: (item[0], item[1], not item[2]), reverse=True)
    best_sim, best_overlap, best_year_conflict, best_ref, best_ref_title = scored[0]
    second_sim = scored[1][0] if len(scored) > 1 else 0.0

    # Rule 2: Exact or high-similarity title match
    if bib_title_norm and len(bib_title_norm) >= policy.min_title_chars_for_exact:
        is_exact = bib_title_norm == best_ref_title or best_sim >= policy.exact_title_threshold
        is_high = best_sim >= policy.high_title_threshold

        if is_exact or is_high:
            # Check tie/ambiguity against second-best candidate
            if len(scored) > 1 and (best_sim - second_sim) < policy.min_margin_over_second:
                return _make_record(
                    bib,
                    groundtruth_arxiv_id=None,
                    decision="uncertain",
                    confidence=best_sim,
                    policy=policy,
                    notes="rule=ambiguous_candidates",
                )

            best_ref_surnames = extract_author_last_names(best_ref.ref_authors)
            both_have_authors = bool(bib_surnames and best_ref_surnames)
            if both_have_authors and best_overlap == 0:
                return _make_record(
                    bib,
                    groundtruth_arxiv_id=None,
                    decision="uncertain",
                    confidence=0.45,
                    policy=policy,
                    notes="rule=title_match_author_mismatch",
                )

            authors_match = bool(bib_surnames and best_ref_surnames and best_overlap > 0)
            exact_title_authors = is_exact and authors_match
            malformed_bib_year = bib.year is not None and not 1900 <= bib.year <= 2026
            if best_year_conflict and not (exact_title_authors and malformed_bib_year):
                return _make_record(
                    bib,
                    groundtruth_arxiv_id=None,
                    decision="uncertain",
                    confidence=0.50,
                    policy=policy,
                    notes="rule=title_match_year_conflict",
                )

            if is_exact:
                conf = min(0.98, 0.90 + 0.08 * best_sim)
                return _make_record(
                    bib,
                    groundtruth_arxiv_id=canonical_id(best_ref.ref_arxiv_id),
                    decision="matched",
                    confidence=conf,
                    policy=policy,
                    notes="rule=exact_normalized_title",
                )

            if both_have_authors and best_overlap >= 1:
                conf = 0.85 + 0.10 * best_sim
                return _make_record(
                    bib,
                    groundtruth_arxiv_id=canonical_id(best_ref.ref_arxiv_id),
                    decision="matched",
                    confidence=conf,
                    policy=policy,
                    notes="rule=fuzzy_title_author_consistent",
                )

    # Rule 3: Clear no-match (only when titles are present and unrelated, and no author overlap)
    if (
        bib_title_norm
        and not any_missing_title
        and max_raw_sim < policy.no_match_title_threshold
        and max_author_overlap == 0
    ):
        return _make_record(
            bib,
            groundtruth_arxiv_id=None,
            decision="unmatched",
            confidence=max(0.80, 1.0 - max_raw_sim),
            policy=policy,
            notes="rule=clear_no_match",
        )

    # Rule 4: Conservative abstention
    return _make_record(
        bib,
        groundtruth_arxiv_id=None,
        decision="uncertain",
        confidence=max_raw_sim,
        policy=policy,
        notes="rule=abstain_ambiguous",
    )


def auto_label(
    bundles: Iterable[PublicationBundle],
    policy: HeuristicPolicy | None = None,
) -> list[LabelRecord]:
    """Generate high-precision heuristic labels for all queries in ready PublicationBundles."""
    active_policy = policy or HeuristicPolicy()
    results: list[LabelRecord] = []
    seen_pubs: set[str] = set()

    for bundle in bundles:
        if bundle.arxiv_id in seen_pubs:
            raise ContractError(f"Duplicate publication in auto_label input: {bundle.arxiv_id}")
        seen_pubs.add(bundle.arxiv_id)
        if bundle.readiness != "ready":
            raise ContractError(
                f"Cannot auto-label non-ready publication {bundle.arxiv_id} (readiness={bundle.readiness})"
            )
        for bib in bundle.bibtex:
            results.append(_label_single_query(bib, bundle.references, active_policy))
    return results


def summarize_auto_labels(labels: Iterable[LabelRecord]) -> dict[str, object]:
    """Summarize heuristic label distribution and rule breakdown for audit reporting."""
    by_decision: dict[str, int] = {"matched": 0, "unmatched": 0, "uncertain": 0}
    by_rule: dict[str, int] = {}
    publications: set[str] = set()
    total = 0

    for label in labels:
        total += 1
        publications.add(label.arxiv_id)
        by_decision[label.decision] = by_decision.get(label.decision, 0) + 1
        note = label.notes or "rule=unknown"
        by_rule[note] = by_rule.get(note, 0) + 1

    decided = by_decision["matched"] + by_decision["unmatched"]
    return {
        "total_queries": total,
        "publication_count": len(publications),
        "by_decision": by_decision,
        "coverage_non_uncertain": round(decided / total, 4) if total else 0.0,
        "by_rule": by_rule,
    }

