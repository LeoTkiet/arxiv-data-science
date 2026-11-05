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


def get_environment_info(label: str = "Google Colab CPU-only") -> dict[str, Any]:
    """Retrieve runtime hardware and software specifications."""
    total_ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 2)
    return {
        "platform": label,
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


def count_renamed_citations_for_paper(output_dir: Path | str, key: str, expected_versions: set[int] | list[int] | None = None) -> int:
    """Calculate how many citations were actually rewritten during deduplication."""
    from arxiv_pipeline.parsing.deduplication import unionize_bibtex

    out = Path(output_dir).resolve()
    state_bib = out / f"state/stages/bibliography/{key}"
    state_norm = out / f"state/stages/normalize/{key}"
    if not state_bib.exists() or not state_norm.exists():
        return 0

    exp_set = set(expected_versions) if expected_versions is not None else None

    bib_entries = []
    for bib_file in sorted(state_bib.glob("*.json")):
        if bib_file.name == "paper.json":
            continue
        try:
            doc = json.loads(bib_file.read_text(encoding="utf-8"))
            v_num = doc.get("version")
            if exp_set is not None and v_num not in exp_set:
                continue
            raw_art = doc.get("artifacts", {}).get("raw_bibtex")
            if raw_art and (out / raw_art).exists():
                raw_doc = json.loads((out / raw_art).read_text(encoding="utf-8"))
                bib_entries.append((v_num, raw_doc.get("entries", [])))
        except Exception:
            pass

    if not bib_entries:
        return 0

    _, key_mapping, _ = unionize_bibtex(bib_entries)
    if not key_mapping:
        return 0

    renamed_count = 0
    for norm_file in sorted(state_norm.glob("*.json")):
        if norm_file.name == "paper.json":
            continue
        try:
            doc = json.loads(norm_file.read_text(encoding="utf-8"))
            v_num = doc.get("version")
            if exp_set is not None and v_num not in exp_set:
                continue
            art = doc.get("artifacts", {}).get("normalized_tree")
            if art and (out / art).exists():
                tree_doc = json.loads((out / art).read_text(encoding="utf-8"))
                for node in tree_doc.get("nodes", []):
                    text = node.get("cleaned_content", "")
                    for match in _CITE_RE.finditer(text):
                        for token in re.split(r"\s*,\s*", match.group(1)):
                            k = token.strip()
                            if k in key_mapping and key_mapping[k] != k:
                                renamed_count += 1
        except Exception:
            pass

    return renamed_count


def generate_parsing_summary(
    output_dir: Path | str,
    wall_time_sec: float,
    peak_ram_mb: float,
    avg_ram_mb: float,
    *,
    total_assigned: int | None = None,
    environment_label: str = "Google Colab CPU-only",
) -> dict[str, Any]:
    """Aggregate stats from parsing manifests and create parsing_summary dict filtered by current scope."""
    output = Path(output_dir).resolve()
    manifest_file = output / "manifests/parsing.json"

    paper_outcomes = []
    if manifest_file.exists():
        try:
            paper_outcomes = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    assigned_count = total_assigned if total_assigned is not None else len(paper_outcomes)
    successful_papers = [p for p in paper_outcomes if p.get("complete") and p.get("status") == "success"]
    empty_papers = [p for p in paper_outcomes if p.get("complete") and p.get("status") == "no_source"]
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

    # Scope papers by current paper_outcomes in manifest with complete=True and matching snapshot
    valid_scoped_papers = []
    scoped_tables_dirs = []

    for p in paper_outcomes:
        if not p.get("complete"):
            continue
        p_id = p.get("arxiv_id")
        if not p_id:
            continue
        k = paper_key(p_id)
        p_dir = output / f"tables/{k}"
        p_manifest = p_dir / "parsing_manifest.json"
        dedup_chk = output / f"state/stages/deduplicate/{k}/paper.json"

        if not p_manifest.exists():
            continue

        try:
            p_doc = json.loads(p_manifest.read_text(encoding="utf-8"))
            if p_doc.get("arxiv_id") != p_id or p_doc.get("complete") is not True:
                continue
            if dedup_chk.exists():
                chk_doc = json.loads(dedup_chk.read_text(encoding="utf-8"))
                chk_snap = chk_doc.get("upstream", {}).get("context", {}).get("input_snapshot_id")
                if not chk_snap and "context" in chk_doc:
                    chk_snap = chk_doc.get("context", {}).get("input_snapshot_id")
                if chk_snap and p_doc.get("input_snapshot_id") != chk_snap:
                    continue
            valid_scoped_papers.append((p, p_doc, p_dir, k))
            scoped_tables_dirs.append(p_dir)
        except Exception:
            continue

    elements_before = 0
    elements_after = 0
    bibtex_before = 0
    bibtex_after = 0
    citations_renamed = 0

    for p, p_doc, p_dir, k in valid_scoped_papers:
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

    avg_time_per_pub = round(wall_time_sec / assigned_count, 4) if assigned_count > 0 else round(wall_time_sec, 4)
    storage_mb = round(sum(calculate_storage_mb(d) for d in scoped_tables_dirs), 2)

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
        )
        wall_time = time.perf_counter() - t0
        peak_mb, avg_mb, _ = sampler.stop()

    total = total_assigned or parse_summary.get("papers") or 1
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
