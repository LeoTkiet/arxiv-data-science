"""Shared configuration; rates apply to one sequential client per run."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class APIConfig:
    endpoint: str = "https://export.arxiv.org/api/query"
    timeout: float = 30.0
    retries: int = 3
    delay: float = 3.0
    user_agent: str = "arxiv-data-science/0.1 (academic corpus discovery)"

    def __post_init__(self):
        if not isfinite(self.timeout) or not isfinite(self.delay) or self.timeout <= 0 or self.retries < 0 or self.delay < 3:
            raise ValueError("timeout > 0, retries >= 0 and delay >= 3 are required")
