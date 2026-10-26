"""Contract v1. All serialized paths are relative to the output root."""

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"
DISCOVERY_STATUSES = {"success", "invalid_id", "not_found", "api_error", "data_error"}
STAGE_STATUSES = {"success", "no_source", "not_indexed", "no_data", "api_error",
                  "data_error", "error", "not_implemented", "skipped"}


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
