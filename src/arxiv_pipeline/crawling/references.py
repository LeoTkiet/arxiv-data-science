"""Stage `references` (TV4): Semantic Scholar bibliography of one paper. See crawling/s2/."""

from pathlib import Path

_sources: dict = {}


def _source(output: Path):
    from .s2.s2_source import S2Source
    key = str(Path(output).resolve())
    if key not in _sources:
        _sources[key] = S2Source(Path(output) / "state" / "s2_last_call")
    return _sources[key]


def run(record, output_dir, *, version=None, upstream=None):
    from .s2.store import store_outcome
    source = _source(output_dir)
    before = source.n_requests
    outcome = next(source.fetch([record["arxiv_id"]]))
    return store_outcome(outcome, output_dir, attempts=max(1, source.n_requests - before))
