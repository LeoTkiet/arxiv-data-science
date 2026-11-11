"""Benchmark and Performance Profiling Harness for Milestone 2 (TV5).

Features:
- Live Pipeline Execution: Executes `pipeline.parse.run(...)` directly under active monitoring.
- Memory Profiling: `MemorySampler` background thread polling process RSS via `psutil` every 0.2s.
- Time Profiling: High-precision `time.perf_counter()` for end-to-end wall time.
- Output Footprint: Computes actual disk usage of generated tables and artifacts strictly within reporting scope.
- Exact Statistics:
  - `parsing_success_rate_pct` calculated strictly on `successfully_parsed_publications`.
  - `citations_renamed_count` computed from actual citation rewriting within expected versions.
  - Scope filtering: filters tables and checkpoints by current manifests and input snapshot.
- Zero Fake Defaults: Eliminates hardcoded synthetic defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from arxiv_pipeline.common.utils import paper_key
from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION
from arxiv_pipeline.reporting.milestone2.validation import safe_path

_CITE_RE = re.compile(
    r"\\(?:cite|citep|citet|citealp|citealt|citeauthor|citeyear|citeyearpar|citenum|nocite|"
    r"parencite|textcite|autocite|footcite|smartcite)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]+)\}"
)


def get_cpu_model() -> str:
    """Detect CPU model on Windows, Linux, or macOS."""
    try:
        if sys.platform == "win32":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            model, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(model).strip()
        elif sys.platform.startswith("linux"):
            cpuinfo = Path("/proc/cpuinfo")
            if cpuinfo.exists():
                for line in cpuinfo.read_text().splitlines():
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def get_environment_info(label: str | None = None) -> dict[str, Any]:
    """Retrieve runtime hardware and software specifications."""
    total_ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 2)
    return {
        "platform": label or platform.platform(),
        "cpu_model": get_cpu_model(),
        "cpu_cores": os.cpu_count() or 1,
        "total_ram_gb": total_ram_gb,
        "python_version": platform.python_version(),
    }


class MemorySampler:
    """Background sampler recording RSS memory usage of process and children every interval."""

    def __init__(self, interval: float = 0.2):
        self.interval = interval
        self._samples: list[float] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = psutil.Process(os.getpid())

    def _sample_loop(self):
        while not self._stop_event.is_set():
            try:
                rss = self._process.memory_info().rss
                for child in self._process.children(recursive=True):
                    try:
                        rss += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self._samples.append(rss / (1024 * 1024))  # Convert to MB
            except Exception:
                pass
            time.sleep(self.interval)

    def start(self):
        self._samples.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> tuple[float, float, list[float]]:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        peak_mb = max(self._samples) if self._samples else 0.0
        avg_mb = (sum(self._samples) / len(self._samples)) if self._samples else 0.0
        return round(peak_mb, 2), round(avg_mb, 2), list(self._samples)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def calculate_storage_mb(directory: Path | str) -> float:
    """Calculate logical file size of all files in directory in MB."""
    p = Path(directory)
    if not p.exists():
        return 0.0
    total_bytes = 0
    for root, _, files in os.walk(p):
        for f in files:
            fp = Path(root) / f
            try:
                if not fp.is_symlink():
                    total_bytes += fp.stat().st_size
            except OSError:
                pass
    return round(total_bytes / (1024 * 1024), 2)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ValueError(f'Cannot read benchmark evidence: {path.name}') from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _verified_stage(output: Path, key: str, stage: str, version: int | None):
    """Read actual Orchestrator checkpoints and verify their recorded byte hashes."""
    relative = f'state/stages/{stage}/{key}/{version or "paper"}.json'
    checkpoint = safe_path(output, relative)
    doc = _read_json(checkpoint)
    meta = _read_json(checkpoint.with_name(checkpoint.stem + '.dependencies.json'))
    if not isinstance(doc, dict) or not isinstance(meta, dict):
        raise ValueError(f'Malformed {stage} checkpoint/dependencies for {key}')
    if (doc.get('stage'), doc.get('version'), doc.get('schema_version')) != (
            stage, version, PARSING_SCHEMA_VERSION) or doc.get('status') not in {'success', 'no_source'}:
        raise ValueError(f'Unready/mismatched {stage} checkpoint for {key}')
    if version is not None and type(doc.get('version')) is not int:
        raise ValueError('Checkpoint version must be an integer')
    if paper_key(doc.get('paper_id', '')) != key:
        raise ValueError(f'Checkpoint paper does not match {key}')
    encoded = json.dumps(doc, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if meta.get('schema_version') != PARSING_SCHEMA_VERSION or meta.get('checkpoint_hash') != hashlib.sha256(encoded).hexdigest():
        raise ValueError(f'Changed {stage} checkpoint for {key}')
    if not isinstance(meta.get('fingerprint'), str) or not meta['fingerprint']:
        raise ValueError(f'Missing {stage} dependency fingerprint for {key}')
    artifacts = doc.get('artifacts')
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError(f'Missing {stage} artifacts for {key}')
    inventory = {}
    for relative in artifacts.values():
        path = safe_path(output, relative)
        if not path.is_file():
            raise ValueError(f'Missing {stage} artifact for {key}')
        inventory[relative] = _sha256(path)
    if inventory != meta.get('artifacts'):
        raise ValueError(f'Changed {stage} artifact for {key}')
    return doc


def count_renamed_citations_for_paper(output_dir: Path | str, key: str,
                                     expected_versions: set[int] | list[int] | None = None) -> int:
    """Count rewritten input key occurrences using the same numeric version order as TV4."""
    from arxiv_pipeline.parsing.deduplication import unionize_bibtex

    output = Path(output_dir).resolve()
    if expected_versions is None:
        dedup = _verified_stage(output, key, 'deduplicate', None)
        manifest = _read_json(safe_path(output, dedup['artifacts']['parsing_manifest']))
        expected_versions = manifest['expected_versions']
    if not isinstance(expected_versions, (list, set, tuple)) or any(type(n) is not int or n < 1 for n in expected_versions):
        raise ValueError("Citation statistics require positive integer versions")
    numbers = sorted(expected_versions)
    if not numbers or len(numbers) != len(set(numbers)):
        raise ValueError('Citation statistics require unique positive version numbers')
    entries, trees = [], []
    for number in numbers:
        for stage, artifact in [('bibliography', 'raw_bibtex'), ('normalize', 'normalized_tree')]:
            checkpoint = _verified_stage(output, key, stage, number)
            payload = _read_json(safe_path(output, checkpoint['artifacts'][artifact]))
            if not isinstance(payload, dict) or (
                payload.get('schema_version'), payload.get('arxiv_id'), payload.get('version')
            ) != (PARSING_SCHEMA_VERSION, checkpoint['paper_id'], number):
                raise ValueError(f'Mismatched {stage} payload for {key} v{number}')
            if stage == 'bibliography':
                entries.append((number, payload['entries']))
            else:
                trees.append(payload['nodes'])
    _, mapping, _ = unionize_bibtex(entries)
    return sum(1 for nodes in trees for node in nodes
               for match in _CITE_RE.finditer(node['cleaned_content'])
               for token in match.group(1).split(',')
               if token.strip() in mapping and mapping[token.strip()] != token.strip())


def _verified_export(output: Path, outcome: dict):
    key = paper_key(outcome['arxiv_id'])
    dedup = _verified_stage(output, key, 'deduplicate', None)
    validation = _verified_stage(output, key, 'validate_parse', None)
    if dedup['artifacts'] != outcome.get('artifacts') or dedup['status'] != outcome.get('status'):
        raise ValueError(f'Paper outcome/export checkpoint mismatch for {key}')
    manifest = _read_json(safe_path(output, dedup['artifacts']['parsing_manifest']))
    report = _read_json(safe_path(output, validation['artifacts']['validation']))
    snapshot = manifest.get('input_snapshot_id')
    if (manifest.get('schema_version'), manifest.get('arxiv_id'), manifest.get('complete')) != (
            PARSING_SCHEMA_VERSION, outcome['arxiv_id'], True):
        raise ValueError(f'Invalid export manifest for {key}')
    if not isinstance(snapshot, str) or not snapshot.strip() or (
        report.get('schema_version'), report.get('arxiv_id'), report.get('input_snapshot_id'), report.get('valid')
    ) != (PARSING_SCHEMA_VERSION, outcome['arxiv_id'], snapshot, True) or report.get('valid') is not True:
        raise ValueError(f'Validation/export snapshot mismatch for {key}')
    if manifest.get('expected_versions') != outcome.get('expected_versions'):
        raise ValueError(f'Export version coverage mismatch for {key}')
    for name in ('elements', 'hierarchy', 'bibtex', 'bibtex_file'):
        if manifest.get('tables', {}).get(name) != dedup['artifacts'].get(name):
            raise ValueError(f'Export table declarations mismatch for {key}')
    return manifest, dedup['artifacts']


def generate_parsing_summary(
    output_dir: Path | str,
    wall_time_sec: float,
    peak_ram_mb: float,
    avg_ram_mb: float,
    *,
    total_assigned: int | None = None,
    environment_label: str | None = None,
) -> dict[str, Any]:
    """Aggregate stats from parsing manifests and create parsing_summary dict filtered by current scope."""
    output = Path(output_dir).resolve()
    manifest_file = output / "manifests/parsing.json"

    paper_outcomes = _read_json(manifest_file)
    if not isinstance(paper_outcomes, list) or any(not isinstance(p, dict) or type(p.get("complete")) is not bool for p in paper_outcomes):
        raise ValueError('Parsing run manifest must be a list of paper outcomes')
    if total_assigned is not None and (type(total_assigned) is not int or total_assigned < 0):
        raise ValueError('total_assigned must be a non-negative integer')
    assigned_count = total_assigned if total_assigned is not None else len(paper_outcomes)
    successful_papers = [p for p in paper_outcomes if p.get("complete") is True and p.get("status") == "success"]
    empty_papers = [p for p in paper_outcomes if p.get("complete") is True and p.get("status") == "no_source"]
    success_count = len(successful_papers)
    empty_count = len(empty_papers)

    # Strictly compute success rate on successfully parsed publications (with source)
    success_rate_pct = round((success_count / assigned_count) * 100, 2) if assigned_count > 0 else 0.0

    total_versions = sum(len(p.get("expected_versions", [])) for p in paper_outcomes)
    parsed_versions = sum(
        len([v for v in p.get("versions", []) if v.get("status") == "success"])
        for p in paper_outcomes
    )
    empty_versions = sum(
        len([v for v in p.get("versions", []) if v.get("status") == "no_source"])
        for p in paper_outcomes
    )

    valid_scoped_papers = []
    exported_paths = set()
    for outcome in paper_outcomes:
        if outcome.get('complete') is not True:
            continue
        manifest, artifacts = _verified_export(output, outcome)
        valid_scoped_papers.append((outcome, manifest, paper_key(outcome['arxiv_id'])))
        exported_paths.update(safe_path(output, relative) for relative in artifacts.values())

    elements_before = 0
    elements_after = 0
    bibtex_before = 0
    bibtex_after = 0
    citations_renamed = 0

    for p, p_doc, k in valid_scoped_papers:
        counts = p_doc.get("counts", {})
        elements_before += counts.get("elements_before", 0)
        elements_after += counts.get("elements_after", 0)
        bibtex_before += counts.get("bibtex_before", 0)
        bibtex_after += counts.get("bibtex_after", 0)

        exp_vers = set(p.get("expected_versions", []))
        citations_renamed += count_renamed_citations_for_paper(output, k, expected_versions=exp_vers)

    elem_reduction_pct = (
        round(((elements_before - elements_after) / elements_before) * 100, 2)
        if elements_before > elements_after else 0.0
    )
    bib_reduction_pct = (
        round(((bibtex_before - bibtex_after) / bibtex_before) * 100, 2)
        if bibtex_before > bibtex_after else 0.0
    )

    avg_time_per_pub = round(wall_time_sec / len(paper_outcomes), 4) if paper_outcomes else 0.0
    storage_mb = round(sum(path.stat().st_size for path in exported_paths) / (1024 * 1024), 2)

    return {
        "environment": get_environment_info(environment_label),
        "coverage_statistics": {
            "total_assigned_publications": assigned_count,
            "successfully_parsed_publications": success_count,
            "pdf_only_empty_publications": empty_count,
            "parsing_success_rate_pct": success_rate_pct,
            "total_versions_discovered": total_versions,
            "parsed_versions_count": parsed_versions,
            "empty_versions_count": empty_versions,
        },
        "deduplication_statistics": {
            "elements_before_dedup": elements_before,
            "elements_after_dedup": elements_after,
            "element_dedup_reduction_pct": elem_reduction_pct,
            "bibtex_entries_before_dedup": bibtex_before,
            "bibtex_entries_after_dedup": bibtex_after,
            "bibtex_dedup_reduction_pct": bib_reduction_pct,
            "citations_renamed_count": citations_renamed,
        },
        "performance_metrics": {
            "end_to_end_wall_time_sec": round(wall_time_sec, 2),
            "avg_time_per_publication_sec": avg_time_per_pub,
            "peak_ram_mb": peak_ram_mb,
            "avg_ram_mb": avg_ram_mb,
            "final_output_storage_mb": storage_mb,
        },
    }


def execute_pipeline_benchmark(
    ms1_input: Path | str,
    output_dir: Path | str,
    *,
    identifiers: list[str] | None = None,
    resume: bool = False,
    summary_out: Path | str | None = None,
    total_assigned: int | None = None,
    frozen_input: bool = False,
    paper_observer=None,
) -> dict[str, Any]:
    """Execute the real pipeline while monitoring time and memory, then generate parsing_summary."""
    from arxiv_pipeline.pipeline.parse import run as run_parse

    out = Path(output_dir).resolve()
    with MemorySampler(0.2) as sampler:
        t0 = time.perf_counter()
        parse_summary = run_parse(
            input_root=ms1_input,
            output=output_dir,
            identifiers=identifiers,
            resume=resume,
            frozen_input=frozen_input,
            paper_observer=paper_observer,
        )
        wall_time = time.perf_counter() - t0
        peak_mb, avg_mb, _ = sampler.stop()

    total = total_assigned if total_assigned is not None else parse_summary["papers"]
    if paper_observer:
        print("Parsing finished. Verifying output for benchmark.", flush=True)
    summary = generate_parsing_summary(
        out,
        wall_time_sec=wall_time,
        peak_ram_mb=peak_mb,
        avg_ram_mb=avg_mb,
        total_assigned=total,
    )

    if summary_out:
        out_path = Path(summary_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Parsing summary saved to: {out_path}")

    return summary


def main():
    parser = argparse.ArgumentParser(description="TV5 Live Benchmark & Performance Profiling")
    parser.add_argument("--ms1-input", help="Directory of MS1 input snapshot (runs live pipeline)")
    parser.add_argument("--output", required=True, help="Directory containing MS2 parsing outputs")
    parser.add_argument("--ids", help="Comma-separated arXiv IDs to parse")
    parser.add_argument("--resume", action="store_true", help="Resume parsing using existing checkpoints")
    parser.add_argument("--summary-out", default="summaries/parsing_summary.json", help="Path to write parsing_summary.json")
    parser.add_argument("--total-assigned", type=int, default=None, help="Total assigned papers for assignment metrics")
    parser.add_argument("--wall-time", type=float, default=None, help="Measured wall time in seconds (if profiling offline)")
    parser.add_argument("--peak-ram", type=float, default=None, help="Peak RAM in MB (if profiling offline)")
    parser.add_argument("--avg-ram", type=float, default=None, help="Avg RAM in MB (if profiling offline)")
    args = parser.parse_args()

    if args.ms1_input:
        ids_list = [i.strip() for i in args.ids.split(",")] if args.ids else None
        execute_pipeline_benchmark(
            ms1_input=args.ms1_input,
            output_dir=args.output,
            identifiers=ids_list,
            resume=args.resume,
            summary_out=args.summary_out,
            total_assigned=args.total_assigned,
        )
    else:
        if args.wall_time is None or args.peak_ram is None or args.avg_ram is None:
            parser.error("Offline profiling requires explicit --wall-time, --peak-ram, and --avg-ram (no synthetic defaults).")
        summary = generate_parsing_summary(
            args.output,
            wall_time_sec=args.wall_time,
            peak_ram_mb=args.peak_ram,
            avg_ram_mb=args.avg_ram,
            total_assigned=args.total_assigned,
        )
        out_path = Path(args.summary_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Summary written to {out_path}")


if __name__ == "__main__":
    main()
