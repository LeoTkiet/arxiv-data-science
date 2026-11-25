"""Scan MS2 ZIPs for additional T3-05 pilot publications."""
import argparse
from pathlib import Path

from arxiv_pipeline.matching.silver_candidate_scan import scan_candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--population", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--roots", type=Path, required=True)
    parser.add_argument("--ranges", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot-per-member", type=int, default=100)
    args = parser.parse_args()
    scan_candidates(args.population, args.selection, args.roots, args.ranges,
                    args.output, pilot_per_member=args.pilot_per_member)


if __name__ == "__main__":
    main()
