"""Atomic matching-state persistence with bounded transient file-lock retries."""
import time

from arxiv_pipeline.common.utils import atomic_json as _write


def atomic_json(path, value):
    for attempt in range(6):
        try:
            return _write(path, value)
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2 ** attempt))
