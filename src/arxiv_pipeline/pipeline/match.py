"""Compatibility entrypoint for the dataset-level matching runner."""

from arxiv_pipeline.matching.pipeline import run_matching


def run(config, output_dir=None, *, version=None, upstream=None):
    if version is not None or upstream is not None:
        raise ValueError('Matching is a dataset-level operation; pass a run config')
    return run_matching(dict(config, output=output_dir) if output_dir is not None else config)
