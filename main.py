"""Checkout entry point; installation is optional."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from arxiv_pipeline.main import main

if __name__ == "__main__":
    raise SystemExit(main())
