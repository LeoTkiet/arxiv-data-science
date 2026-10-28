"""Corpus Extractor module for Milestone 1 (TV3).

Complies with docs/CONTRACTS.md and the project specification:
- Safe extraction preventing Zip-Slip / Path Traversal attacks.
- Supports tar, tar.gz, gzip (single .tex), and raw .tex sources.
- Filters and retains only LaTeX (.tex) and BibTeX (.bib) files.
- Removes binary, image, and auxiliary build files without modifying LaTeX source code.
- Version-isolated storage under corpus/<paper_key>/vN/.
- Atomic extraction via temporary staging directory.
- Detailed classification of extraction status: 'success', 'no_source', 'data_error', 'error'.
"""

import gzip
import logging
import os
from pathlib import Path
import shutil
import tarfile
from typing import Any

from arxiv_pipeline.common.schemas import StageResult, ErrorInfo
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now

LOG = logging.getLogger("arxiv_pipeline")

ALLOWED_EXTENSIONS = {".tex", ".bib"}


class ExtractionSecurityError(ValueError):
    """Raised when an archive member attempts path traversal or directory escaping."""
    pass


class ExtractionDataError(ValueError):
    """Raised when archive payload is corrupted, invalid or unreadable."""
    pass


def is_safe_path(base_dir: Path, target_path: Path) -> bool:
    """Verify that target_path resolves strictly within base_dir."""
    try:
        resolved_base = base_dir.resolve()
        resolved_target = target_path.resolve()
        return resolved_target.is_relative_to(resolved_base)
    except (ValueError, RuntimeError):
        return False


def _check_tar_member(member: tarfile.TarInfo, dest_dir: Path) -> Path:
    """Validate a tar member to prevent path traversal or unsafe file types."""
    # Disallow absolute paths, drive letters, and parent references
    name = member.name.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ExtractionSecurityError(f"Absolute path in tar archive: {member.name}")
    parts = Path(name).parts
    if ".." in parts:
        raise ExtractionSecurityError(f"Path traversal '..' in tar member: {member.name}")
    
    target_path = dest_dir / member.name
    if not is_safe_path(dest_dir, target_path):
        raise ExtractionSecurityError(f"Target path escapes destination: {member.name}")

    # Check symlinks or hardlinks
    if member.issym() or member.islnk():
        link_target = dest_dir / member.linkname
        if not is_safe_path(dest_dir, link_target):
            raise ExtractionSecurityError(f"Link target escapes destination: {member.linkname}")

    return target_path


def safe_extract_tar(tar_path: Path, dest_dir: Path) -> list[Path]:
    """Safely extract tar / tar.gz archive into dest_dir with path traversal checks."""
    extracted_files: list[Path] = []
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar.getmembers():
                target_path = _check_tar_member(member, dest_dir)
                if member.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    f_in = tar.extractfile(member)
                    if f_in is not None:
                        with open(target_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                        extracted_files.append(target_path)
    except (ExtractionSecurityError, tarfile.TarError, EOFError, OSError) as exc:
        if isinstance(exc, ExtractionSecurityError):
            raise
        raise ExtractionDataError(f"Tar extraction failed: {exc}") from exc

    return extracted_files


def safe_extract_gz(gz_path: Path, dest_dir: Path, output_filename: str = "main.tex") -> list[Path]:
    """Decompress a standalone .gz file (typically a compressed single .tex)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_path = dest_dir / output_filename
    if not is_safe_path(dest_dir, target_path):
        raise ExtractionSecurityError(f"Escaping destination for gz: {output_filename}")

    try:
        with gzip.open(gz_path, "rb") as f_in, open(target_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ExtractionDataError(f"Gzip decompression failed: {exc}") from exc

    return [target_path]


def safe_extract_plain_tex(source_path: Path, dest_dir: Path, output_filename: str = "main.tex") -> list[Path]:
    """Handle uncompressed plain text / LaTeX source."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target_path = dest_dir / output_filename
    if not is_safe_path(dest_dir, target_path):
        raise ExtractionSecurityError(f"Escaping destination: {output_filename}")

    try:
        shutil.copy2(source_path, target_path)
    except OSError as exc:
        raise ExtractionDataError(f"Failed to copy plain source: {exc}") from exc

    return [target_path]


def filter_corpus(dest_dir: Path) -> tuple[list[Path], int, int]:
    """Filter files in dest_dir, keeping only .tex and .bib.
    
    Removes images, binaries, and auxiliary files.
    Cleans up any empty subdirectories.
    
    Returns:
        (kept_files, raw_total_bytes, filtered_total_bytes)
    """
    kept_files: list[Path] = []
    raw_total_bytes = 0
    filtered_total_bytes = 0

    # First pass: collect and filter files
    for root, _, files in os.walk(dest_dir):
        for file in files:
            file_path = Path(root) / file
            try:
                size = file_path.stat().st_size
                raw_total_bytes += size
                if file_path.suffix.lower() in ALLOWED_EXTENSIONS:
                    kept_files.append(file_path)
                    filtered_total_bytes += size
                else:
                    # Remove non-tex/non-bib files (images, binaries, logs, etc.)
                    file_path.unlink()
            except OSError:
                pass

    # Second pass: clean up empty directories (bottom-up)
    for root, dirs, _ in os.walk(dest_dir, topdown=False):
        for d in dirs:
            dir_path = Path(root) / d
            try:
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
            except OSError:
                pass

    return kept_files, raw_total_bytes, filtered_total_bytes


def extract_source(source_path: Path, corpus_dir: Path) -> tuple[str, list[Path], dict[str, Any]]:
    """Extract and filter raw source into corpus_dir using atomic staging.
    
    Returns:
        (status, kept_files, metadata_dict)
    """
    if not source_path.exists() or source_path.stat().st_size == 0:
        return "no_source", [], {"reason": "source file does not exist or is empty"}

    # Peek format from header
    with open(source_path, "rb") as f:
        header = f.read(1024)

    if header.startswith(b"%PDF-"):
        return "no_source", [], {"reason": "source is PDF-only, no LaTeX source available"}

    staging_dir = corpus_dir.parent / f".{corpus_dir.name}.staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Decompress / unpack
        if header.startswith(b"\x1f\x8b"):
            # Could be tar.gz or standalone .gz
            try:
                safe_extract_tar(source_path, staging_dir)
            except (ExtractionDataError, tarfile.TarError):
                # Fallback to single gzip file
                safe_extract_gz(source_path, staging_dir)
        elif tarfile.is_tarfile(source_path):
            safe_extract_tar(source_path, staging_dir)
        elif b"\\documentclass" in header or b"\\input" in header or b"%" in header:
            safe_extract_plain_tex(source_path, staging_dir)
        else:
            # Try tar anyway
            try:
                safe_extract_tar(source_path, staging_dir)
            except Exception:
                # Try single gz
                try:
                    safe_extract_gz(source_path, staging_dir)
                except Exception:
                    # Treat as plain text
                    safe_extract_plain_tex(source_path, staging_dir)

        # 2. Filter .tex and .bib
        kept_files, raw_bytes, corpus_bytes = filter_corpus(staging_dir)

        if not kept_files:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return "no_source", [], {
                "reason": "archive contains no valid .tex or .bib files",
                "raw_size_bytes": raw_bytes,
            }

        rel_files = [p.relative_to(staging_dir) for p in kept_files]

        # 3. Atomic commit: move staging to final corpus_dir
        if corpus_dir.exists():
            shutil.rmtree(corpus_dir, ignore_errors=True)
        corpus_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir.rename(corpus_dir)

        final_kept_files = [corpus_dir / r for r in rel_files]
        meta = {
            "extracted_files": [str(r).replace("\\", "/") for r in rel_files],
            "raw_size_bytes": raw_bytes,
            "corpus_size_bytes": corpus_bytes,
            "file_count": len(final_kept_files),
            "extracted_at": utc_now(),
        }
        return "success", final_kept_files, meta

    except ExtractionSecurityError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise ExtractionDataError(str(exc)) from exc


def run(record: dict[str, Any], output_dir: Path, *, version: dict[str, Any] | None = None,
        upstream: dict[str, Any] | None = None) -> StageResult:
    """Adapter entrypoint for stage 'extract'."""
    paper_id = record["arxiv_id"]
    version_num = version["version"] if version else None
    corpus_rel = version.get("corpus_path", f"corpus/{paper_key(paper_id)}/v{version_num}")
    corpus_dir = output_dir / corpus_rel

    if upstream and upstream.get("status") == "no_source":
        return StageResult(
            stage="extract",
            paper_id=paper_id,
            version=version_num,
            status="no_source",
            timestamp=utc_now(),
            artifacts={},
            error=ErrorInfo("no_source", "Downstream source was classified as no_source/PDF-only"),
            attempts=1,
            size_bytes=0,
        )

    source_rel = None
    if upstream and upstream.get("artifacts"):
        source_rel = upstream["artifacts"].get("source")
    if not source_rel:
        source_rel = version.get("source_path", f"raw/{paper_key(paper_id)}/v{version_num}/source")

    source_path = output_dir / source_rel

    try:
        status, kept_files, meta = extract_source(source_path, corpus_dir)
        if status == "success":
            # Save sidecar metadata
            meta_path = corpus_dir / "extract_metadata.json"
            atomic_json(meta_path, meta)
            return StageResult(
                stage="extract",
                paper_id=paper_id,
                version=version_num,
                status="success",
                timestamp=utc_now(),
                artifacts={"corpus": corpus_rel},
                attempts=1,
                size_bytes=meta.get("corpus_size_bytes", 0),
            )
        else:
            return StageResult(
                stage="extract",
                paper_id=paper_id,
                version=version_num,
                status="no_source",
                timestamp=utc_now(),
                artifacts={},
                error=ErrorInfo("no_tex_source", meta.get("reason", "No TeX/BibTeX sources found")),
                attempts=1,
                size_bytes=0,
            )
    except ExtractionSecurityError as exc:
        LOG.error("Security traversal violation during extraction for %s v%s: %s", paper_id, version_num, exc)
        return StageResult(
            stage="extract",
            paper_id=paper_id,
            version=version_num,
            status="data_error",
            timestamp=utc_now(),
            artifacts={},
            error=ErrorInfo("security_violation", str(exc)),
            attempts=1,
            size_bytes=0,
        )
    except ExtractionDataError as exc:
        LOG.error("Corrupted archive data for %s v%s: %s", paper_id, version_num, exc)
        return StageResult(
            stage="extract",
            paper_id=paper_id,
            version=version_num,
            status="data_error",
            timestamp=utc_now(),
            artifacts={},
            error=ErrorInfo("corrupted_archive", str(exc)),
            attempts=1,
            size_bytes=0,
        )
    except Exception as exc:
        LOG.exception("Unexpected error extracting %s v%s: %s", paper_id, version_num, exc)
        return StageResult(
            stage="extract",
            paper_id=paper_id,
            version=version_num,
            status="error",
            timestamp=utc_now(),
            artifacts={},
            error=ErrorInfo(type(exc).__name__, str(exc)),
            attempts=1,
            size_bytes=0,
        )
