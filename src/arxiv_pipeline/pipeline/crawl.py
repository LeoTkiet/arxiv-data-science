"""Durable orchestration. Modules are loaded only when crawl is requested."""

from collections import Counter
import hashlib
import importlib
import json
import logging
from time import perf_counter
from pathlib import Path

from arxiv_pipeline.common.schemas import SCHEMA_VERSION, StageResult, ErrorInfo
from arxiv_pipeline.common.utils import atomic_json, utc_now, paper_key
from arxiv_pipeline.crawling.discovery import ArxivClient, discover

LOG = logging.getLogger("arxiv_pipeline")
MODULES = {"download": "arxiv_pipeline.crawling.downloader", "extract": "arxiv_pipeline.crawling.extractor",
           "metadata": "arxiv_pipeline.crawling.metadata", "references": "arxiv_pipeline.crawling.references",
           "validate": "arxiv_pipeline.export.validation"}


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _complete_discovery(record, identifier):
    if not isinstance(record, dict):
        return False
    try:
        from arxiv_pipeline.common.utils import normalize_id
        base, requested = normalize_id(identifier)
        versions = record["versions"]
        count = record["metadata"]["latest_version"]
        return (record["schema_version"] == SCHEMA_VERSION and record["status"] == "success"
                and record["input_id"] == identifier and record["arxiv_id"] == base
                and record["requested_version"] == requested and record["error"] is None
                and type(count) is int and count > 0
                and [v["version"] for v in versions] == list(range(1, count + 1))
                and all(v["versioned_id"] == f"{base}v{v['version']}" and v["source_path"] and v["corpus_path"]
                        for v in versions))
    except (KeyError, TypeError, ValueError):
        return False


def _artifacts_exist(result, output):
    try:
        for relative in result["artifacts"].values():
            path = (output / relative).resolve()
            if Path(relative).is_absolute() or not path.is_relative_to(output.resolve()) or not path.exists():
                return False
        return True
    except (TypeError, AttributeError, ValueError):
        return False


def _run_stage(stage, record, output, version, upstream, resume, observer=None):
    started = perf_counter()
    number = version["version"] if version else None
    path = output / "state/stages" / stage / paper_key(record["arxiv_id"]) / f"{number or 'paper'}.json"
    cached = _read(path) if resume else None
    # Empty outcomes are terminal; failures and unimplemented modules are retried.
    if ((upstream is None or upstream["status"] == "success")
            and isinstance(cached, dict) and cached.get("schema_version") == SCHEMA_VERSION
            and cached.get("stage") == stage and cached.get("paper_id") == record["arxiv_id"]
            and cached.get("version") == number
            and cached.get("status") in {"success", "no_source", "not_indexed", "no_data"}
            and _artifacts_exist(cached, output)):
        if cached["status"] != "success" or cached.get("artifacts"):
            if observer:
                observer(dict(kind='stage', stage=stage, paper_id=record['arxiv_id'], version=number,
                              status=cached['status'], cached=True, elapsed_seconds=perf_counter() - started))
            return cached
    if upstream and upstream["status"] != "success":
        result = StageResult(stage, record["arxiv_id"], number, "skipped", utc_now(),
            error=ErrorInfo("upstream_unavailable", f"download status: {upstream['status']}"))
    else:
        try:
            module = importlib.import_module(MODULES[stage])
            result = module.run(record, output, version=version, upstream=upstream)
            if not isinstance(result, StageResult):
                raise TypeError("Adapter must return StageResult")
            if (result.stage, result.paper_id, result.version) != (stage, record["arxiv_id"], number):
                raise ValueError("Adapter returned mismatched stage/paper/version")
            if not _artifacts_exist(result.to_dict(), output):
                raise ValueError("Adapter artifact must exist inside output directory")
            if result.status == "success" and not result.artifacts:
                raise ValueError("Successful adapter must provide an artifact")
        except NotImplementedError as exc:
            result = StageResult(stage, record["arxiv_id"], number, "not_implemented", utc_now(),
                                 error=ErrorInfo("module_pending", str(exc)))
        except Exception as exc:
            LOG.exception("Stage %s failed for %s", stage, record["arxiv_id"])
            result = StageResult(stage, record["arxiv_id"], number, "error", utc_now(),
                                 error=ErrorInfo(type(exc).__name__, str(exc)))
    serialized = result.to_dict()
    atomic_json(path, serialized)
    if observer:
        observer(dict(kind='stage', stage=stage, paper_id=record['arxiv_id'], version=number,
                      status=serialized['status'], cached=False, elapsed_seconds=perf_counter() - started))
    return serialized


def run(identifiers, output: Path, *, client=None, resume=False, full=False, observer=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    client = client or ArxivClient()
    records, stages = [], []
    for identifier in dict.fromkeys(identifiers):
        paper_started = perf_counter()
        discovery_started = perf_counter()
        key = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        checkpoint = output / "state/discovery" / f"{key}.json"
        record = _read(checkpoint) if resume else None
        cached = _complete_discovery(record, identifier)
        if not cached:
            record = discover(identifier, client).to_dict()
            atomic_json(checkpoint, record)
        else:
            LOG.info("Resume discovery: %s", identifier)
        if observer:
            observer(dict(kind='discovery', paper_id=record['arxiv_id'], input_id=identifier,
                          status=record['status'], cached=cached, elapsed_seconds=perf_counter() - discovery_started))
        records.append(record)
        LOG.info("Discovery %s: %s (%d versions)", identifier, record["status"], len(record["versions"]))
        if not full or record["status"] != "success":
            if observer:
                observer(dict(kind='paper', paper_id=record['arxiv_id'], input_id=identifier,
                              elapsed_seconds=perf_counter() - paper_started))
            continue
        for version in record["versions"]:
            downloaded = _run_stage("download", record, output, version, None, resume, observer)
            stages.append(downloaded)
            stages.append(_run_stage("extract", record, output, version, downloaded, resume, observer))
        for stage in ("metadata", "references", "validate"):
            stages.append(_run_stage(stage, record, output, None, None, resume, observer))
        if observer:
            observer(dict(kind='paper', paper_id=record['arxiv_id'], input_id=identifier,
                          elapsed_seconds=perf_counter() - paper_started))
    atomic_json(output / "manifests/discovery.json", records)
    atomic_json(output / "manifests/stages.json", stages)
    bad = any(r["status"] != "success" for r in records)
    bad |= any(s["status"] not in {"success", "no_source", "not_indexed", "no_data"}
               and not (s["status"] == "skipped" and s.get("error", {}).get("message") == "download status: no_source")
               for s in stages)
    summary = {"schema_version": SCHEMA_VERSION, "timestamp": utc_now(), "mode": "crawl" if full else "discovery",
               "papers": len({r["arxiv_id"] for r in records if r["arxiv_id"]}), "inputs": len(records),
               "versions": sum(len(r["versions"]) for r in records if r["status"] == "success"),
               "discovery_statuses": dict(Counter(r["status"] for r in records)),
               "stage_statuses": dict(Counter(s["status"] for s in stages)), "complete": not bad}
    atomic_json(output / "summary.json", summary)
    return summary
