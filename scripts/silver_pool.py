"""T3-05 selection and read-only bundle preparation CLI."""
import argparse
import json
from pathlib import Path

from arxiv_pipeline.matching.silver_pool import prepare_pool, select_pool


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--population", type=Path, required=True)
    select.add_argument("--ranges", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--seed", type=int, default=42)
    select.add_argument("--reserve-per-member", type=int, default=30)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--selection", type=Path, required=True)
    prepare.add_argument("--roots", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "select":
        result = select_pool(args.population, args.ranges, args.output,
                             seed=args.seed, reserve_per_member=args.reserve_per_member)
        print(json.dumps({"selected": sum(map(len, result["selected"].values())),
                          "minimum": result["minimum_auto_count"], "output": str(args.output)}))
    else:
        result = prepare_pool(args.selection, args.roots, args.output)
        print(json.dumps({"ready": result["ready"], "not_ready": result["not_ready"],
                          "output": str(args.output / 'prepared_manifest.json')}))


if __name__ == "__main__":
    main()
