"""Store a Semantic Scholar Outcome in the pipeline layout and return its StageResult.

Writes references/<paper_key>.json, references/<paper_key>.provenance.json and the stage
checkpoint state/stages/references/<paper_key>/paper.json, exactly where the orchestrator
looks when it resumes, and appends one line per attempt to state/s2_progress.jsonl (same
format as S2Crawler's progress.jsonl). Used by both the per-paper adapter and the batch tool.
"""
import json
from pathlib import Path

from arxiv_pipeline.common.schemas import ErrorInfo, StageResult
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

from .s2_source import Outcome

STAGE = "references"
STATUS_MAP = {"ok": "success", "ok_empty": "no_data", "not_found": "not_indexed", "error": "api_error"}
TERMINAL = {"success", "no_source", "not_indexed", "no_data"}  

def checkpoint_path(output: Path, arxiv_id: str) -> Path:
    return Path(output) / "state/stages" / STAGE / paper_key(arxiv_id) / "paper.json"


def progress_path(output: Path) -> Path:
    return Path(output) / "state" / "s2_progress.jsonl"


def log_progress(output: Path, outcome: Outcome) -> None:
    """Append-only log, one line per attempt; the latest line of an ID is its current state."""
    path = progress_path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"arxiv_id": outcome.arxiv_id, "status": outcome.status,
                                 "n_refs": len(outcome.references), "paginated": outcome.paginated,
                                 "seconds": round(outcome.seconds, 3)}) + "\n")


def reference_row(arxiv_id: str, ref: dict) -> dict:
    """One reference in the CONTRACTS.md layout (Semantic Scholar ID never becomes an arXiv ID)."""
    row = {
        "arxiv_id": arxiv_id,
        "ref_arxiv_id": ref.get("arxiv_id"),
        "ref_title": ref.get("title"),
        "ref_authors": list(ref.get("authors") or []),
        "ref_submission_date": ref.get("submission_date"),
        "ref_s2_id": ref.get("s2_id"),
    }
    row["missing_fields"] = [k for k, v in row.items() if k != "arxiv_id" and v in (None, [])]
    return row


def store_outcome(outcome: Outcome, output, *, attempts: int = 1) -> StageResult:
    output, key = Path(output), paper_key(outcome.arxiv_id)
    status = STATUS_MAP[outcome.status]
    artifacts, size, error = {}, 0, None
    if outcome.status == "error":
        error = ErrorInfo("s2_unavailable", outcome.error or "Semantic Scholar request failed", retryable=True)
    else:
        rows = [reference_row(outcome.arxiv_id, r) for r in outcome.references]
        refs_rel, prov_rel = f"references/{key}.json", f"references/{key}.provenance.json"
        provenance = {
            "source": "semantic_scholar", "retrieved_at": utc_now(), "status": status,
            "s2_paper_id": outcome.s2_paper_id, "venue": outcome.venue,
            "reference_count": len(rows), "paginated": outcome.paginated,
        }
        atomic_json(output / refs_rel, rows)
        atomic_json(output / prov_rel, provenance)
        artifacts = {"references": refs_rel, "provenance": prov_rel}
        size = sum((output / p).stat().st_size for p in artifacts.values())
    result = StageResult(STAGE, outcome.arxiv_id, None, status, utc_now(), artifacts=artifacts,
                         error=error, attempts=attempts, size_bytes=size)
    atomic_json(checkpoint_path(output, outcome.arxiv_id), result.to_dict())
    log_progress(output, outcome)
    return result
