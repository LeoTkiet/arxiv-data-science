"""Export compact benchmark evidence to the separate Jupyter Book repository."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from arxiv_pipeline.common.utils import atomic_json, utc_now


def export_run(run_dir, book_dir, name):
    if not name or Path(name).name != name or '\\' in name or name in {'.', '..'}:
        raise ValueError('Run name must be a single directory name')
    source = Path(run_dir).resolve()
    target = Path(book_dir).resolve() / 'summaries/milestone1' / name
    if target.exists():
        raise ValueError('Export destination already exists; use a new run name')
    files = ('benchmark.json', 'statistics.json', 'samples.jsonl', 'timings.jsonl')
    payloads = {name: (source / name).read_bytes() for name in files}
    benchmark = json.loads(payloads['benchmark.json'])
    summary = json.loads(payloads['statistics.json'])
    if benchmark['label'] != summary['label']:
        raise ValueError('Benchmark/statistics run labels disagree')
    # Public book needs measurements, not local machine paths/argv.
    for field in ('command', 'output_dir'):
        benchmark.pop(field, None)
    benchmark.get('environment', {}).pop('executable', None)
    target.mkdir(parents=True)
    atomic_json(target / 'benchmark.json', benchmark)
    atomic_json(target / 'statistics.json', summary)
    for name in ('samples.jsonl', 'timings.jsonl'):
        shutil.copyfile(source / name, target / name)
    atomic_json(target / 'provenance.json', dict(exported_at=utc_now(), label=benchmark['label'],
        original_sha256={name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
        note='benchmark.json excludes local command, output path and Python executable; retain originals privately'))
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--book', type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args(argv)
    export_run(args.run, args.book, args.name)
    print('Export completed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
