"""CLI logging to stderr and output/logs/pipeline.log."""

import logging
from pathlib import Path


def configure_logging(output: Path):
    (output / "logs").mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("arxiv_pipeline")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    for handler in (logging.StreamHandler(), logging.FileHandler(output / "logs/pipeline.log", encoding="utf-8")):
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def close_logging(logger):
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
