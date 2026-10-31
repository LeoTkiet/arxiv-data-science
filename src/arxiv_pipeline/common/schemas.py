"""MS1 contract 1.0 and MS2 table rows; MS2 opts into version 2.0 explicitly."""

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"
# Independent from MS1 checkpoints: opt in explicitly for parsing artifacts/results.
PARSING_SCHEMA_VERSION = "2.0"
DISCOVERY_STATUSES = {"success", "invalid_id", "not_found", "api_error", "data_error"}
STAGE_STATUSES = {"success", "no_source", "not_indexed", "no_data", "api_error",
                  "data_error", "error", "not_implemented", "skipped"}


@dataclass
class ElementRow:
    """Distinct cleaned element in one publication, shared across versions."""

    element_id: str
    arxiv_id: str
    kind: str
    content: str


@dataclass
class HierarchyRow:
    """The four official columns; root nodes have no row of their own."""

    arxiv_id: str
    version: int
    element_id: str
    parent_id: str


@dataclass
class BibtexRow:
    """Unified reference after field union and citation-key reconciliation."""

    arxiv_id: str
    citation_key: str
    entry_type: str
    title: str
    authors: list[str]
    year: int | None
    doi: str | None = None
    journal: str | None = None
    url: str | None = None


@dataclass
class ParsedNode:
    """Represents a node in the intermediate hierarchical syntax tree (raw_tree)."""

    temp_id: str
    parent_temp_id: str | None
    position: int
    kind: str
    raw_content: str
    level: int | None = None
    heading_rank: int | None = None

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "temp_id": self.temp_id,
            "parent_temp_id": self.parent_temp_id,
            "position": self.position,
            "kind": self.kind,
            "raw_content": self.raw_content,
        }
        if self.level is not None:
            res["level"] = self.level
        if self.heading_rank is not None:
            res["heading_rank"] = self.heading_rank
        return res


@dataclass
class ErrorInfo:
    kind: str
    message: str
    retryable: bool = False
    http_status: int | None = None


@dataclass
class VersionRecord:
    version: int
    versioned_id: str
    source_url: str
    source_path: str
    corpus_path: str
    submission_date: str
    updated_date: str
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    venue: str | None = None


@dataclass
class DiscoveryRecord:
    input_id: str
    arxiv_id: str | None
    requested_version: int | None
    status: str
    timestamp: str
    metadata: dict[str, Any] | None = None
    versions: list[VersionRecord] = field(default_factory=list)
    error: ErrorInfo | None = None
    attempts: int = 0
    schema_version: str = SCHEMA_VERSION

    def to_dict(self):
        return asdict(self)


@dataclass
class StageResult:
    """Return type required from TV2/TV3/TV4/TV5 adapters."""

    stage: str
    paper_id: str
    version: int | None
    status: str
    timestamp: str
    artifacts: dict[str, str] = field(default_factory=dict)
    error: ErrorInfo | None = None
    attempts: int = 0
    size_bytes: int = 0
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"Unknown stage status: {self.status}")

    def to_dict(self):
        return asdict(self)
