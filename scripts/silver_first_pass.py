"""Generate draft T3-05 heuristic labels from prepared bundles."""
import argparse
import json
from pathlib import Path

from arxiv_pipeline.matching.silver_first_pass import run_first_pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--population", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_first_pass(args.prepared, args.population, args.output)
    print(json.dumps({key: result[key] for key in
                      ("prepared_ready", "candidate_full_coverage_publications",
                       "uncertain_entries", "minimum_auto_publications", "ready_for_handoff")}))


if __name__ == "__main__":
    main()
