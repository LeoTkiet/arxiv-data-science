# Feature Dictionary & Rationale — Milestone 3 Reference Matching (TV2)

- **Owner**: TV2 (Cleaning, Candidate Generation, Feature Engineering & Baseline)
- **Reviewers**: TV4 (Supervised Models), TV5 (Evaluation & Metrics)
- **Schema ID**: `5176b5c594d38654b01ab708ab15b9483ccc7a35a1057397e80d9fbd7025ba50`
- **Schema Version**: `v3.0`
- **Module Implementation**: `src/arxiv_pipeline/matching/cleaning.py`, `candidates.py`, `features.py`, `baseline.py`
- **Schema JSON Artifact**: `docs/matching/feature_schema.json`

---

## 1. Pipeline Invariants & Architecture

1. **Within-Publication Pairing Invariant**:
   - Matching is strictly performed between BibTeX entries and crawled references of the **exact same publication** (`arxiv_id`).
   - Pair candidate space is Cartesian $m \times n$ where $m = |\text{bibtex}|$ and $n = |\text{references}|$.
   - Any cross-publication pairing raises `ContractError`.
   - Empty candidate sets ($n=0$) or empty queries ($m=0$) are gracefully handled without crashing.

2. **Strict Anti-Leakage Invariant**:
   - Feature extraction only uses metadata fields available at inference time.
   - Pair features **never** leak candidate IDs, label sources, confidence scores, or ground truth labels into $X$.
   - Feature column order is strictly fixed and validated against `FeatureSchema`.

3. **Bit-Consistent Determinism**:
   - All string distance calculations (Levenshtein ratio via DP with indel distance, char 3-gram cosine, Jaccard) are mathematically deterministic across Python environments, independent of C-extensions or external platform variations.

---

## 2. Text Cleaning & Normalization Policy (`cleaning.py`)

| Component | Policy & Transformation Rule | Rationale |
| :--- | :--- | :--- |
| **Unicode Normalization** | Unicode `NFKC` | Recombines accent ligatures, canonicalizes fullwidth characters, ensures consistent character representations. |
| **Case Handling** | `.casefold()` | Superior to `.lower()` for multilingual strings and non-ASCII characters. |
| **LaTeX Accents** | Regex conversion (`\`{e}` $\to$ `è`, `\"{o}` $\to$ `ö`, `\c{c}` $\to$ `ç`) | Restores true phonetic names corrupted by LaTeX TeX macros into standard Unicode. |
| **LaTeX Macros & Braces** | Unwraps `\textbf`, `\emph`, `\textit`, etc., and protective BibTeX `{...}` | Strips formatting artifacts while preserving actual semantic title words. |
| **Inline Math** | Replaced with whitespace (`$[...]$`, `\([...]\)`) | Math expressions inject noisy tokens that degrade word overlap metrics. |
| **Stopword Policy** | **Preserved** in `normalize_title` | Preserving stopwords maintains exact title equality (e.g. *"Attention Is All You Need"*), title length ratios, and contiguous n-grams. Jaccard and cosine handle term distribution. |
| **Author Parsing** | Split on `and` / `;`, extract `last_name`, `first_name`, and `initials` | Normalizes both *"Last, First"* and *"First Last"*. Strips *"et al."* markers without dropping the author entry. Does not assume identically named authors are identical individuals. |
| **Year Parsing** | Regex extraction of 4-digit integers constrained to `[1900, 2030]` | Tolerates full ISO-8601 timestamps (e.g. `2021-09-15T12:00:00Z`), strings, and numeric types while rejecting out-of-bounds noise. |
| **Canonical DOI** | Lowercase, strip `https://doi.org/`, `http://dx.doi.org/`, `doi:` prefixes | Ensures exact string matching regardless of DOI URL formatting variants. |
| **arXiv ID Extraction** | Modern (`YYMM.NNNNN[vN]`) and legacy (`archive/YYMMNNN[vN]`) extraction | Extracted from BibTeX `url`, `journal`, or `citation_key` via canonical `normalize_id`. |

---

## 3. Registered Feature Schema (16 Features)

Every feature is registered in `get_feature_schema()` and validated via `contracts.validate_features`.

| # | Feature Name | Group | Dtype | Range | Missing Policy | Source Fields | Rationale & Description |
| :-: | :--- | :--- | :-: | :-: | :--- | :--- | :--- |
| 1 | `title_exact_match` | Title | `int64` | `[0, 1]` | Defaults to 0 | `title`, `ref_title` | Indicator (1/0) of exact match between normalized BibTeX title and candidate reference title. |
| 2 | `title_jaccard_word` | Title | `float64` | `[0.0, 1.0]` | Defaults to 0.0 | `title`, `ref_title` | Token-level Jaccard similarity between word sets of normalized titles: $\frac{\|A \cap B\|}{\|A \cup B\|}$. |
| 3 | `title_levenshtein_ratio` | Title | `float64` | `[0.0, 1.0]` | Defaults to 0.0 | `title`, `ref_title` | Character-level normalized Levenshtein similarity ratio: $\frac{m+n-\text{dist}}{m+n}$. Resilient to minor typos and OCR errors. |
| 4 | `title_char_ngram_cosine` | Title | `float64` | `[0.0, 1.0]` | Defaults to 0.0 | `title`, `ref_title` | Cosine similarity between character 3-gram term-frequency vectors of normalized titles. Robust to word transposition. |
| 5 | `title_len_diff_ratio` | Title | `float64` | `[0.0, 1.0]` | Defaults to 1.0 | `title`, `ref_title` | Relative title length discrepancy: $\frac{\|len_1 - len_2\|}{\max(len_1, len_2, 1)}$. High values penalize truncated or mismatched titles. |
| 6 | `author_jaccard_last` | Author | `float64` | `[0.0, 1.0]` | Defaults to 0.0 | `authors`, `ref_authors` | Jaccard similarity over sets of standardized author surnames. |
| 7 | `first_author_match` | Author | `int64` | `[0, 1]` | Defaults to 0 | `authors`, `ref_authors` | Exact match indicator between the primary/first author's surname. Strong signal in academic citations. |
| 8 | `author_count_diff` | Author | `int64` | `[0, 100]` | Defaults to 0 | `authors`, `ref_authors` | Absolute difference in parsed author counts, capped at 100. Useful indicator for paper identity. |
| 9 | `doi_exact_match` | Identifier | `int64` | `[0, 1]` | Defaults to 0 | `doi` | Exact match indicator for canonical DOIs. High-precision signal when available. |
| 10 | `has_doi_both` | Identifier | `int64` | `[0, 1]` | Defaults to 0 | `doi` | Indicator flag whether both BibTeX and candidate reference possess DOI metadata. |
| 11 | `arxiv_id_match` | Identifier | `int64` | `[0, 1]` | Defaults to 0 | `url`, `journal`, `key`, `ref_arxiv_id` | Direct match indicator between arXiv ID extracted from BibTeX free-text/url and candidate `ref_arxiv_id`. Precision $\approx 100\%$. |
| 12 | `year_exact_match` | Temporal | `int64` | `[0, 1]` | Defaults to 0 | `year`, `ref_submission_date` | Indicator flag whether publication years are identical. |
| 13 | `year_diff_abs` | Temporal | `float64` | `[0.0, 100.0]` | Imputed to 99.0 | `year`, `ref_submission_date` | Absolute gap between publication years. Distant citations are strongly penalized. |
| 14 | `year_is_chronological` | Temporal | `int64` | `[0, 1]` | Defaults to 1 | `year`, `ref_submission_date`, `arxiv_id` | Physical validity check: $year(\text{reference}) \le year(\text{citing})$. Non-chronological citations are penalized. |
| 15 | `is_bib_year_missing` | Temporal | `int64` | `[0, 1]` | Flag (0 or 1) | `year` | Missingness flag indicating absence of publication year in BibTeX. Allows ML model to learn missingness bias. |
| 16 | `is_ref_year_missing` | Temporal | `int64` | `[0, 1]` | Flag (0 or 1) | `ref_submission_date` | Missingness flag indicating absence of submission/publication date in reference record. |

---

## 4. Deterministic Baseline Matcher (`baseline.py`)

The deterministic baseline serves as an un-cheatable benchmark for MRR@5 comparison with supervised ML models (TV4) and official evaluation (TV5).

### 4.1 Scoring Logic (`score_pair_heuristics`)
- **Tier 1 (Direct Exact Identifiers)**:
  - If `arxiv_id_match == 1`: Score = `1.0`
  - If `doi_exact_match == 1`: Score = `0.98`
- **Tier 2 (Exact Cleaned Title)**:
  - Base score = `0.90`
  - If `first_author_match == 1`: Boosted to `0.95`
- **Tier 3 (Weighted Heuristic Combination)**:
  $$\text{Score} = 0.50 \cdot \text{title\_ngram\_cosine} + 0.20 \cdot \text{title\_jaccard} + 0.15 \cdot \text{author\_jaccard} + 0.05 \cdot \text{first\_author} + 0.10 \cdot \text{year\_exact}$$
  - **Penalties**:
    - If `year_is_chronological == 0`: Multiply score by `0.5`
    - If `title_len_diff_ratio > 0.5`: Multiply score by $(1.0 - 0.5 \cdot \text{len\_diff})$

### 4.2 Ranking & Tie-Breaking (`rank_top5_predictions`)
- For every query $(arxiv\_id, citation\_key)$, candidates are sorted by:
  1. `score` **DESCENDING**
  2. `ref_arxiv_id` **ASCENDING** (deterministic tie-break)
- Output format: Top-5 candidate arXiv IDs padded with `None` if fewer than 5 candidates exist. Conforms 100% to `RankedPrediction` and `contracts.validate_predictions`.

---

## 5. Ablation Study & Feature Importance Placeholder (Task T2-05)

When real data parsing completes and G3/G4 begins:
- **Feature Group Importance**: Ablation tests will measure MRR@5 drops upon dropping Title, Author, Identifier, and Temporal feature groups using `python -m arxiv_pipeline.matching.analysis`.
- **Error Taxonomy**: Detailed slice analysis on high-frequency error modes:
  - Incomplete reference metadata (missing title / missing year)
  - Common author surname collision (e.g., Wang, Zhang, Smith)
  - Pre-print vs camera-ready title alterations
