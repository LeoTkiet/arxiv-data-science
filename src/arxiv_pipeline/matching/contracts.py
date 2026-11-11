"""M3 contract 3.0: typed boundaries and validation, independent of ML libraries.

This module does not load files, generate candidates, fit models or choose splits.
Raw M1/M2 rows enter through publication_from_rows; downstream modules exchange
keyed records. Metadata belongs in manifests, never in published prediction rows.
"""

from dataclasses import asdict, dataclass, field, fields
import hashlib
import json
import math
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from arxiv_pipeline.common.schemas import BibtexRow, MATCHING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import normalize_id

BibtexInput = BibtexRow
QueryKey = tuple[str, str]
ReferenceKey = tuple[str, str]
PairKey = tuple[str, str, str]
PARTITIONS = ("train", "valid", "test")
POOLS = ("manual", "auto")
_LABEL_FIELDS = {"groundtruth_arxiv_id", "label_source", "confidence", "annotator",
                 "reviewer", "review_status", "heuristic_version", "notes", "decision", "y", "label"}
_RESERVED_FEATURES = _LABEL_FIELDS | {"arxiv_id", "citation_key", "ref_arxiv_id", "partition"}


class ContractError(ValueError):
    """Malformed or incompatible input at a module boundary."""


def canonical_id(value: str) -> str:
    if not isinstance(value, str):
        raise ContractError("arXiv ID must be a string")
    try:
        return normalize_id(value)[0]
    except ValueError as exc:
        raise ContractError(str(exc)) from exc


def _text(value: str, name: str):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a nonempty string")


def _number(value):
    try:
        valid = type(value) in (float, int) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ContractError("Expected a finite numeric value (not bool)")


def _matches(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any:
        return True
    if origin in (Union, UnionType):
        return any(_matches(value, part) for part in args)
    if origin is list:
        return isinstance(value, list) and all(_matches(v, args[0]) for v in value)
    if origin is dict:
        return isinstance(value, dict) and all(_matches(k, args[0]) and _matches(v, args[1])
                                               for k, v in value.items())
    if annotation is float:
        return type(value) in (int, float)
    if annotation is int:
        return type(value) is int and -(2**63) <= value < 2**63
    if annotation is bool:
        return type(value) is bool
    return isinstance(value, annotation)


def _validate_fields(record):
    hints = get_type_hints(type(record))
    for f in fields(record):
        if not _matches(getattr(record, f.name), hints[f.name]):
            raise ContractError(f"Invalid type for {type(record).__name__}.{f.name}")


def _digest(payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _Record:
    def __post_init__(self):
        _validate_fields(self)
        for name in ("arxiv_id", "ref_arxiv_id", "groundtruth_arxiv_id",
                     "cand_1", "cand_2", "cand_3", "cand_4", "cand_5"):
            value = getattr(self, name, None)
            if value is not None:
                object.__setattr__(self, name, canonical_id(value))
        if getattr(self, "citation_key", None) is not None:
            _text(self.citation_key, "citation_key")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ReferenceInput(_Record):
    arxiv_id: str
    ref_arxiv_id: str
    ref_title: str
    ref_authors: list[str]
    ref_submission_date: str | None = None
    ref_s2_id: str | None = None

    @property
    def key(self) -> ReferenceKey:
        return self.arxiv_id, self.ref_arxiv_id


@dataclass(frozen=True)
class Issue(_Record):
    arxiv_id: str
    stage: str
    code: str
    severity: str
    message: str
    citation_key: str | None = None
    ref_arxiv_id: str | None = None
    source_path: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.severity not in {"warning", "error"}:
            raise ContractError("Invalid issue severity")


@dataclass(frozen=True)
class PublicationBundle(_Record):
    arxiv_id: str
    bibtex: list[BibtexInput]
    references: list[ReferenceInput]
    readiness: str
    issues: list[Issue] = field(default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        if self.readiness not in {"ready", "not_ready", "invalid"}:
            raise ContractError("Invalid bundle readiness")
        # BibtexInput reuses the M2 dataclass, whose constructor is intentionally lenient.
        object.__setattr__(self, "bibtex", [bibtex_input(row) for row in self.bibtex])
        seen_queries, seen_refs = set(), set()
        for row in self.bibtex + self.references:
            if row.arxiv_id != self.arxiv_id:
                raise ContractError("Row belongs to a different publication")
            key = (row.arxiv_id, row.citation_key) if isinstance(row, BibtexRow) else row.key
            seen = seen_queries if isinstance(row, BibtexRow) else seen_refs
            if key in seen:
                raise ContractError(f"duplicate key: {key}")
            seen.add(key)
        if any(i.arxiv_id != self.arxiv_id for i in self.issues):
            raise ContractError("Issue belongs to a different publication")
        if self.readiness == "ready" and any(i.severity == "error" for i in self.issues):
            raise ContractError("Ready publication contains blocking issues")


def bibtex_input(row: BibtexRow | dict) -> BibtexInput:
    """Validate a copy of an M2 row without changing the M2 dataclass/schema."""
    if not isinstance(row, (BibtexRow, dict)):
        raise ContractError("BibtexInput must be an M2 row object or mapping")
    data = asdict(row) if isinstance(row, BibtexRow) else dict(row)
    try:
        result = BibtexInput(**data)
    except TypeError as exc:
        raise ContractError(f"Invalid BibtexInput fields: {exc}") from exc
    _validate_fields(result)
    result.arxiv_id = canonical_id(result.arxiv_id)
    _text(result.citation_key, "citation_key")
    _text(result.entry_type, "entry_type")
    return result


def publication_from_rows(arxiv_id, bibtex_rows, reference_rows, *, readiness):
    """Adapt in-memory M1/M2 rows. Caller must establish readiness from upstream.

    Raw references without arXiv IDs stay in the original snapshot. Issues record
    their exclusion; identical candidates collapse; conflicting duplicates fail.
    This boundary is not a replacement for the future snapshot/file loader.
    """
    paper = canonical_id(arxiv_id)
    bibtex = [bibtex_input(row) for row in bibtex_rows]
    references, issues = {}, []
    for raw in reference_rows:
        if not isinstance(raw, dict) or canonical_id(raw.get("arxiv_id")) != paper:
            raise ContractError("Reference belongs to a different publication")
        if "ref_arxiv_id" not in raw:
            raise ContractError("Missing ref_arxiv_id column")
        if raw["ref_arxiv_id"] is None:
            issues.append(Issue(paper, "match_input", "non_arxiv_reference", "warning",
                                "Reference excluded from candidates: no arXiv ID"))
            continue
        data = {f.name: raw[f.name] for f in fields(ReferenceInput) if f.name in raw}
        if "ref_title" in data and data["ref_title"] is None:
            data["ref_title"] = ""
            issues.append(Issue(paper, "match_input", "missing_reference_title", "warning",
                                "M1 null title normalized to empty string",
                                ref_arxiv_id=raw["ref_arxiv_id"]))
        try:
            ref = ReferenceInput(**data)
        except TypeError as exc:
            raise ContractError(f"Invalid ReferenceInput fields: {exc}") from exc
        if ref.key in references:
            if references[ref.key] != ref:
                raise ContractError(f"Reference metadata conflict: {ref.key}")
            issues.append(Issue(paper, "match_input", "duplicate_reference", "warning",
                                "Identical normalized candidate collapsed; source rows retained upstream",
                                ref_arxiv_id=ref.ref_arxiv_id))
        references[ref.key] = ref
    return PublicationBundle(paper, bibtex, list(references.values()), readiness, issues)


@dataclass(frozen=True)
class LabelRecord(_Record):
    arxiv_id: str
    citation_key: str
    groundtruth_arxiv_id: str | None
    decision: str
    label_source: str
    confidence: float | None = None
    annotator: str | None = None
    reviewer: str | None = None
    review_status: str = "pending"
    heuristic_version: str | None = None
    notes: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.decision not in {"matched", "unmatched", "uncertain"}:
            raise ContractError("Invalid label decision")
        if (self.decision == "matched") != (self.groundtruth_arxiv_id is not None):
            raise ContractError("Only matched decisions must have a groundtruth ID")
        if self.label_source not in {"manual", "heuristic"}:
            raise ContractError("Invalid label_source (use manual or heuristic)")
        if self.review_status not in {"pending", "accepted", "rejected"}:
            raise ContractError("Invalid review_status")
        if self.confidence is not None:
            _number(self.confidence)
            if not 0 <= self.confidence <= 1:
                raise ContractError("confidence must be in [0,1]")
        if self.label_source == "heuristic":
            if self.confidence is None:
                raise ContractError("Heuristic labels require confidence")
            _text(self.heuristic_version, "heuristic_version")

    @property
    def key(self) -> QueryKey:
        return self.arxiv_id, self.citation_key

    @property
    def is_supervised(self):
        return self.review_status == "accepted" and self.decision != "uncertain"


@dataclass(frozen=True)
class PairFeature(_Record):
    arxiv_id: str
    citation_key: str
    ref_arxiv_id: str
    values: dict[str, float | int | bool | None]

    def __post_init__(self):
        super().__post_init__()
        if set(self.values) & _RESERVED_FEATURES:
            raise ContractError("Reserved column in feature values")
        for value in self.values.values():
            if value is not None and not isinstance(value, bool):
                _number(value)

    @property
    def key(self) -> PairKey:
        return self.arxiv_id, self.citation_key, self.ref_arxiv_id

    def to_dict(self):
        return dict(arxiv_id=self.arxiv_id, citation_key=self.citation_key,
                    ref_arxiv_id=self.ref_arxiv_id, **self.values)


@dataclass(frozen=True)
class FeatureSpec(_Record):
    name: str
    dtype: str
    nullable: bool
    minimum: float | None
    maximum: float | None
    missing_policy: str
    rationale: str
    source_fields: list[str]

    def __post_init__(self):
        super().__post_init__()
        for name in ("name", "missing_policy", "rationale"):
            _text(getattr(self, name), name)
        if self.name in _RESERVED_FEATURES or set(self.source_fields) & _LABEL_FIELDS:
            raise ContractError("Feature may leak identifiers or labeling metadata")
        if self.dtype not in {"float64", "int64", "bool"}:
            raise ContractError("Features must have numeric/bool dtype")
        for value in (self.minimum, self.maximum):
            if value is not None:
                _number(value)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ContractError("Feature minimum exceeds maximum")
        if not self.source_fields or any(not s.strip() for s in self.source_fields):
            raise ContractError("Feature must document source fields")


@dataclass(frozen=True)
class FeatureSchema(_Record):
    features: list[FeatureSpec]
    schema_version: str = MATCHING_SCHEMA_VERSION

    def __post_init__(self):
        super().__post_init__()
        if not self.features or len({f.name for f in self.features}) != len(self.features):
            raise ContractError("Feature schema must have unique, nonempty columns")
        if self.schema_version != MATCHING_SCHEMA_VERSION:
            raise ContractError("Incompatible matching schema version")

    @property
    def schema_id(self):
        # Feature order matters: the model matrix must use this exact order.
        return _digest(asdict(self))


@dataclass(frozen=True)
class FeatureBatch(_Record):
    rows: list[PairFeature]
    feature_schema_id: str

    def __post_init__(self):
        super().__post_init__()
        _text(self.feature_schema_id, "feature_schema_id")

    def to_dict(self):
        return dict(rows=[r.to_dict() for r in self.rows], feature_schema_id=self.feature_schema_id)


@dataclass(frozen=True)
class ScoreRecord(_Record):
    arxiv_id: str
    citation_key: str
    ref_arxiv_id: str
    score: float

    def __post_init__(self):
        super().__post_init__()
        _number(self.score)

    @property
    def key(self) -> PairKey:
        return self.arxiv_id, self.citation_key, self.ref_arxiv_id


@dataclass(frozen=True)
class RankedPrediction(_Record):
    arxiv_id: str
    partition: str
    citation_key: str
    groundtruth_arxiv_id: str | None
    cand_1: str | None = None
    cand_2: str | None = None
    cand_3: str | None = None
    cand_4: str | None = None
    cand_5: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.partition not in PARTITIONS:
            raise ContractError("partition must be train, valid or test")
        candidates = [getattr(self, f"cand_{i}") for i in range(1, 6)]
        present = [x for x in candidates if x is not None]
        if candidates != present + [None] * (5 - len(present)):
            raise ContractError("Candidate nulls must be trailing")
        if len(present) != len(set(present)):
            raise ContractError("Duplicate ranked candidate")

    @property
    def key(self) -> QueryKey:
        return self.arxiv_id, self.citation_key


@dataclass(frozen=True)
class PartitionRow(_Record):
    arxiv_id: str
    partition: str
    pool: str

    def __post_init__(self):
        super().__post_init__()
        if self.partition not in PARTITIONS or self.pool not in POOLS:
            raise ContractError("Invalid partition or label pool")


@dataclass(frozen=True)
class PartitionManifest(_Record):
    seed: int
    group_size: int
    population_snapshot_id: str
    label_snapshot_id: str
    manual_publication_ids: list[str]
    auto_publication_ids: list[str]
    rows: list[PartitionRow]
    schema_version: str = MATCHING_SCHEMA_VERSION

    def __post_init__(self):
        super().__post_init__()
        if self.seed < 0 or self.group_size < 1 or self.schema_version != MATCHING_SCHEMA_VERSION:
            raise ContractError("Invalid seed, group size or schema version")
        _text(self.population_snapshot_id, "population_snapshot_id")
        _text(self.label_snapshot_id, "label_snapshot_id")
        for name in ("manual_publication_ids", "auto_publication_ids"):
            ids = [canonical_id(v) for v in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ContractError("duplicate publication in pool")
            object.__setattr__(self, name, ids)
        manual, auto = set(self.manual_publication_ids), set(self.auto_publication_ids)
        if manual & auto:
            raise ContractError("Manual/auto publication pools overlap")
        if len({r.arxiv_id for r in self.rows}) != len(self.rows):
            raise ContractError("duplicate publication across partitions")
        if {r.arxiv_id for r in self.rows} != manual | auto:
            raise ContractError("Partition rows do not cover the label pools")
        for row in self.rows:
            if row.arxiv_id not in (manual if row.pool == "manual" else auto):
                raise ContractError("Partition row pool disagrees with label pool")

    @property
    def counts(self):
        return {p: {pool: sum(r.partition == p and r.pool == pool for r in self.rows)
                    for pool in POOLS} for p in PARTITIONS}

    @property
    def split_id(self):
        payload = asdict(self)
        for name in ("manual_publication_ids", "auto_publication_ids"):
            payload[name] = sorted(payload[name])
        payload["rows"] = sorted(payload["rows"], key=lambda r: r["arxiv_id"])
        return _digest(payload)

    def to_dict(self):
        return dict(asdict(self), counts=self.counts, split_id=self.split_id)


@dataclass(frozen=True)
class ValidationReport:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def valid(self):
        return not self.errors

    @property
    def query_keys(self):
        return list(dict.fromkeys((i.arxiv_id, i.citation_key) for i in self.errors
                                  if i.citation_key is not None))

    def to_dict(self):
        return dict(asdict(self), valid=self.valid, query_keys=[list(k) for k in self.query_keys])


def validate_labels(bundles: list[PublicationBundle], labels: list[LabelRecord]) -> ValidationReport:
    """Check label FK/uniqueness without inventing labels for absent queries."""
    errors, warnings, queries, targets, seen = [], [], set(), {}, set()
    for bundle in bundles:
        if bundle.arxiv_id in targets:
            errors.append(Issue(bundle.arxiv_id, "labels", "duplicate_publication", "error",
                                "Publication appears more than once"))
        if bundle.readiness != "ready":
            errors.append(Issue(bundle.arxiv_id, "labels", "input_not_ready", "error",
                                "Publication has not passed upstream readiness checks"))
        targets[bundle.arxiv_id] = {r.ref_arxiv_id for r in bundle.references}
        queries.update((b.arxiv_id, b.citation_key) for b in bundle.bibtex)
        warnings.extend(bundle.issues)
    for label in labels:
        code = None
        if label.key in seen:
            code = "duplicate_label"
        elif label.key not in queries:
            code = "unknown_query"
        elif label.groundtruth_arxiv_id is not None and label.groundtruth_arxiv_id not in targets[label.arxiv_id]:
            code = "groundtruth_outside_candidates"
        if code:
            errors.append(Issue(label.arxiv_id, "labels", code, "error", code,
                                citation_key=label.citation_key))
        seen.add(label.key)
    return ValidationReport(errors, warnings, dict(queries=len(queries), labeled=len(seen & queries),
        supervised=len({r.key for r in labels if r.is_supervised and r.key in queries}),
        unlabeled=len(queries - seen)))


def validate_features(batch: FeatureBatch, schema: FeatureSchema) -> ValidationReport:
    errors, seen = [], set()
    specs = {spec.name: spec for spec in schema.features}
    # Empty batches still carry a schema ID; use a contract-level exception because
    # there is no publication ID to attach a row-level Issue to.
    if batch.feature_schema_id != schema.schema_id and not batch.rows:
        raise ContractError("Feature schema ID mismatch")
    for row in batch.rows:
        reasons = []
        if batch.feature_schema_id != schema.schema_id:
            reasons.append("feature_schema_mismatch")
        if row.key in seen:
            reasons.append("duplicate_pair")
        seen.add(row.key)
        if set(row.values) != set(specs):
            reasons.append("feature_columns_mismatch")
        else:
            for name, value in row.values.items():
                spec = specs[name]
                expected = {"float64": float, "int64": int, "bool": bool}[spec.dtype]
                if value is None:
                    if not spec.nullable:
                        reasons.append(f"missing_feature:{name}")
                elif not _matches(value, expected):
                    reasons.append(f"feature_dtype:{name}")
                elif (spec.minimum is not None and value < spec.minimum or
                      spec.maximum is not None and value > spec.maximum):
                    reasons.append(f"feature_range:{name}")
        errors.extend(Issue(row.arxiv_id, "features", reason, "error", reason,
                            citation_key=row.citation_key, ref_arxiv_id=row.ref_arxiv_id)
                      for reason in reasons)
    return ValidationReport(errors, counts={"pairs": len(batch.rows)})


def validate_split_quotas(manifest: PartitionManifest, *, population_count: int) -> ValidationReport:
    """Validate group publication quotas; entry/annotator coverage is TV3's audit.

    population_count must come from the assigned population snapshot, not merely
    the subset currently parsed. No split creation or random resampling occurs.
    """
    if type(population_count) is not int or population_count < len(manifest.rows):
        raise ContractError("population_count must cover all selected publications")
    k, errors = manifest.group_size, []
    manual, auto = len(manifest.manual_publication_ids), len(manifest.auto_publication_ids)
    reasons = []
    if manual < 5 * k:
        reasons.append("manual_publication_quota")
    if auto < max(math.ceil((population_count - manual) / 10), 2 * k):
        reasons.append("auto_publication_quota")
    for partition in ("valid", "test"):
        for pool in POOLS:
            if manifest.counts[partition][pool] != k:
                reasons.append(f"{partition}_{pool}_quota")
    # Errors refer to an actual member, never to a fabricated global arXiv ID.
    if reasons and not manifest.rows:
        raise ContractError("Empty partition manifest cannot satisfy group quotas")
    for reason in reasons:
        errors.append(Issue(manifest.rows[0].arxiv_id, "split", reason, "error", reason))
    return ValidationReport(errors, counts={"manual": manual, "auto": auto})


def _query_registry(bundles, stage):
    queries, errors = {}, []
    seen = set()
    for bundle in bundles:
        if bundle.arxiv_id in seen or bundle.readiness != "ready":
            code = "duplicate_publication" if bundle.arxiv_id in seen else "input_not_ready"
            errors.append(Issue(bundle.arxiv_id, stage, code, "error", code))
        seen.add(bundle.arxiv_id)
        targets = {r.ref_arxiv_id for r in bundle.references}
        queries.update({(b.arxiv_id, b.citation_key): targets for b in bundle.bibtex})
    return queries, errors


def validate_scores(bundles: list[PublicationBundle], scores: list[ScoreRecord]) -> ValidationReport:
    """Require exactly one finite score per candidate, retaining empty queries."""
    queries, errors = _query_registry(bundles, "scores")
    seen, scored = set(), {}
    for score in scores:
        query = score.arxiv_id, score.citation_key
        code = None
        if score.key in seen:
            code = "duplicate_score"
        elif query not in queries:
            code = "unknown_query"
        elif score.ref_arxiv_id not in queries[query]:
            code = "candidate_outside_publication"
        try:
            _number(score.score)
        except ContractError:
            code = "nonfinite_score"
        if code:
            errors.append(Issue(score.arxiv_id, "scores", code, "error", code,
                                citation_key=score.citation_key, ref_arxiv_id=score.ref_arxiv_id))
        seen.add(score.key)
        scored.setdefault(query, set()).add(score.ref_arxiv_id)
    for query, targets in queries.items():
        missing = targets - scored.get(query, set())
        if missing:
            errors.append(Issue(query[0], "scores", "missing_scores", "error",
                                f"Missing {len(missing)} candidate scores", citation_key=query[1]))
    return ValidationReport(errors, counts={"queries": len(queries), "scores": len(scores),
                                            "expected_scores": sum(len(v) for v in queries.values())})


def validate_predictions(bundles: list[PublicationBundle], predictions: list[RankedPrediction],
                         labels: list[LabelRecord], manifest: PartitionManifest) -> ValidationReport:
    """Check prediction coverage, partition membership, candidate FK and label join.

    This validates already ranked output; it does not rank scores or compute MRR.
    Only accepted resolved labels supply ground truth. Missing/uncertain labels
    remain distinguishable in the separate label registry.
    """
    queries, errors = _query_registry(bundles, "predictions")
    partitions = {r.arxiv_id: r.partition for r in manifest.rows}
    available = {b.arxiv_id for b in bundles}
    for paper in partitions.keys() - available:
        errors.append(Issue(paper, "predictions", "missing_publication", "error",
                            "Split publication has no input bundle"))
    label_report = validate_labels(bundles, labels)
    errors.extend(label_report.errors)
    truth = {r.key: r.groundtruth_arxiv_id for r in labels if r.is_supervised}
    expected = {q for q in queries if q[0] in partitions}
    seen = set()
    for row in predictions:
        reasons = []
        if row.key in seen:
            reasons.append("duplicate_prediction")
        seen.add(row.key)
        if row.key not in expected:
            reasons.append("query_outside_split")
        else:
            if row.partition != partitions[row.arxiv_id]:
                reasons.append("partition_mismatch")
            candidates = [getattr(row, f"cand_{i}") for i in range(1, 6)
                          if getattr(row, f"cand_{i}") is not None]
            if set(candidates) - queries[row.key]:
                reasons.append("candidate_outside_publication")
            if len(candidates) != min(5, len(queries[row.key])):
                reasons.append("candidate_count_mismatch")
            if row.groundtruth_arxiv_id != truth.get(row.key):
                reasons.append("groundtruth_mismatch")
        errors.extend(Issue(row.arxiv_id, "predictions", code, "error", code,
                            citation_key=row.citation_key) for code in reasons)
    for paper, key in sorted(expected - seen):
        errors.append(Issue(paper, "predictions", "missing_prediction", "error",
                            "Query must survive even with zero candidates", citation_key=key))
    return ValidationReport(errors, label_report.warnings,
                            counts={"expected_queries": len(expected), "predictions": len(predictions)})
