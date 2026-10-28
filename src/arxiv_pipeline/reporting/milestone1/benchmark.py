"""Benchmark a separate pipeline process, retaining raw samples and failure evidence."""
import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import hashlib
from uuid import uuid4

import psutil

from arxiv_pipeline.common.utils import atomic_json, utc_now


def disk_bytes(directory):
    """Logical file bytes, including staging/partial files; never follows symlinks."""
    total, unavailable = 0, 0
    for current, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # A staging file may be renamed during sampling.
            except OSError:
                unavailable += 1
    return total, unavailable


def timing_summary(path):
    events, diagnostics = [], []
    if path.exists():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError) as exc:
            lines = []
            diagnostics.append(dict(line=None, error=str(exc)))
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('kind') not in {'discovery', 'stage', 'paper'}:
                    raise ValueError('Invalid timing event kind')
                elapsed = event.get('elapsed_seconds')
                if type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError('Invalid elapsed_seconds')
                if event['kind'] == 'stage' and not isinstance(event.get('stage'), str):
                    raise ValueError('Stage name is missing')
                for field in ('paper_id', 'input_id'):
                    if event.get(field) is not None and not isinstance(event[field], str):
                        raise ValueError(f'Invalid {field}')
                if event['kind'] == 'paper' and not (event.get('paper_id') or event.get('input_id')):
                    raise ValueError('Paper identity is missing')
                events.append(event)
            except (ValueError, TypeError) as exc:
                diagnostics.append(dict(line=number, error=str(exc)))
    papers = [e for e in events if e['kind'] == 'paper']
    stages = {}
    for event in events:
        if event['kind'] == 'stage':
            stages[event['stage']] = stages.get(event['stage'], 0) + event['elapsed_seconds']
    # Aliases for one paper contribute to the same paper's measured work.
    per_paper = {}
    for event in papers:
        key = event.get('paper_id') or event['input_id']
        per_paper[key] = per_paper.get(key, 0) + event['elapsed_seconds']
    return dict(discovery_total_seconds=sum(e['elapsed_seconds'] for e in events if e['kind'] == 'discovery'),
                stage_seconds=stages, per_paper_seconds=per_paper,
                mean_paper_seconds=statistics.mean(per_paper.values()) if per_paper else None,
                cached_events=sum(bool(e.get('cached')) for e in events), event_count=len(events),
                diagnostics=diagnostics)


def current_snapshot(output, run_id):
    """Verify current-run receipt and hashes, including on a completed exit=1 run."""
    receipt = json.loads((output / 'manifests/run.json').read_text(encoding='utf-8'))
    expected = ('manifests/discovery.json', 'manifests/stages.json', 'summary.json')
    if receipt.get('run_id') != run_id:
        raise ValueError('No completed snapshot for this run_id; previous manifest is not current data')
    for name in expected:
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        if receipt.get('sha256', {}).get(name) != digest:
            raise ValueError(f'Snapshot hash mismatch: {name}')
    return receipt


def terminate_tracked(process, tracked):
    """Stop observed descendants even if their original parent has disappeared."""
    candidates = dict(tracked)
    if process is not None:
        try:
            root = psutil.Process(process.pid)
            candidates.update((child.pid, child) for child in root.children(recursive=True))
            candidates[root.pid] = root
        except psutil.NoSuchProcess:
            pass
    errors = []
    for child in candidates.values():
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            errors.append(str(exc))
    _, alive = psutil.wait_procs(list(candidates.values()), timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            errors.append(str(exc))
    _, remaining = psutil.wait_procs(alive, timeout=3)
    return dict(errors=errors, remaining_pids=[child.pid for child in remaining])


def measure(command, output_dir, results_dir, *, interval=0.1, label='live_unverified'):
    """Measure subprocess wall time, sampled summed RSS and output-root logical bytes.

    Results must be new and outside the corpus; stdout/stderr and sampler artifacts
    are excluded from disk measurements. Returns a report even if launch/command fails.
    RSS includes shared pages once per process (may double-count shared mappings).
    Sampled extrema are observed peaks, not guaranteed instantaneous high-water marks.
    """
    output, results = Path(output_dir).resolve(), Path(results_dir).resolve()
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('Sampling interval must be finite and positive')
    if results.exists() or results.is_relative_to(output) or output.is_relative_to(results):
        raise ValueError('Results must be a new directory separate from the output tree')
    results.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    initial_bytes, _ = disk_bytes(output)
    started = time.perf_counter()
    packages = {dist.metadata['Name']: dist.version for dist in importlib.metadata.distributions()
                if dist.metadata['Name']}
    source = Path(__file__).resolve().parents[4]
    git = ['git', '-c', f'safe.directory={source.as_posix()}', '-C', str(source)]
    try:
        commit = subprocess.check_output(git + ['rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(git + ['status', '--porcelain'], stderr=subprocess.DEVNULL, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    report = dict(schema_version='1.0', started_at=utc_now(), label=label, command=list(command),
        sample_interval_seconds=interval, output_dir=str(output),
        environment=dict(python=sys.version, executable=sys.executable, platform=platform.platform(),
            cpu=platform.processor(), logical_cpu_count=psutil.cpu_count(),
            total_ram_bytes=psutil.virtual_memory().total, packages=packages,
            is_colab=bool(os.environ.get('COLAB_RELEASE_TAG') or os.environ.get('COLAB_BACKEND_VERSION')),
            cpu_only_confirmation='Must be selected and verified in the Colab notebook',
            source_commit=commit, source_dirty=dirty),
        disk=dict(initial_bytes=initial_bytes), exit_code=None, max_observed_processes=0)
    atomic_json(results / 'run.json', report)
    samples, inaccessible, process = [], 0, None
    tracked = {}
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3]) + os.pathsep + env.get('PYTHONPATH', '')
    # Environment values (including API credentials) are never persisted.
    try:
        with (results / 'pipeline.log').open('wb') as log, (results / 'samples.jsonl').open('w', encoding='utf-8') as raw:
            run_started = time.perf_counter()
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while True:
                rss, count = 0, 0
                try:
                    root = psutil.Process(process.pid)
                    processes = [root, *root.children(recursive=True)]
                    for child in processes:
                        tracked[child.pid] = child
                except psutil.NoSuchProcess:
                    processes = []
                except psutil.AccessDenied:
                    processes = []
                    inaccessible += 1
                alive_descendants = 0
                for child in list(tracked.values()):
                    try:
                        if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                            tracked.pop(child.pid, None)
                            continue
                        rss += child.memory_info().rss
                        count += 1
                        alive_descendants += child.pid != process.pid
                    except psutil.NoSuchProcess:
                        tracked.pop(child.pid, None)
                    except psutil.AccessDenied:
                        inaccessible += 1
                size, unavailable = disk_bytes(output)
                inaccessible += unavailable
                sample = dict(elapsed_seconds=time.perf_counter() - run_started,
                              rss_bytes=rss if count else None, process_count=count, disk_bytes=size)
                samples.append(sample)
                raw.write(json.dumps(sample) + '\n')
                raw.flush()
                report['max_observed_processes'] = max(report['max_observed_processes'], count)
                if process.poll() is not None and not alive_descendants:
                    break
                time.sleep(interval)
            report['exit_code'] = process.wait()
            report['wall_seconds'] = time.perf_counter() - run_started
    except OSError as exc:
        report['exit_code'] = 127
        report['launch_error'] = str(exc)
        report['cleanup'] = terminate_tracked(process, tracked)
    except KeyboardInterrupt:
        report['exit_code'] = 130
        report['interrupted'] = True
        report['cleanup'] = terminate_tracked(process, tracked)
    finally:
        report.setdefault('wall_seconds', time.perf_counter() - started)
        rss_samples = [s['rss_bytes'] for s in samples if s['rss_bytes'] is not None]
        weighted_sum = weight = 0.0
        for a, b in zip(samples, samples[1:]):
            if a['rss_bytes'] is not None:
                duration = b['elapsed_seconds'] - a['elapsed_seconds']
                weighted_sum += a['rss_bytes'] * duration
                weight += duration
        final, disk_errors = disk_bytes(output)
        report['disk'].update(final_bytes=final,
                              peak_observed_bytes=max([initial_bytes, final, *(s['disk_bytes'] for s in samples)]))
        report['ram'] = dict(peak_rss_bytes=max(rss_samples) if rss_samples else None,
            time_weighted_mean_rss_bytes=weighted_sum / weight if weight else None,
            observed_seconds=weight)
        report['sample_count'] = len(samples)
        report['measurement_access_errors'] = inaccessible + disk_errors
        report['finished_at'] = utc_now()
        report['timings'] = timing_summary(results / 'timings.jsonl')
        atomic_json(results / 'benchmark.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--interval', type=float, default=0.1)
    parser.add_argument('--demo', action='store_true', help='Offline synthetic integration example, not graded results')
    parser.add_argument('pipeline_args', nargs=argparse.REMAINDER, help='After --: pipeline --ids/--input/--range/--resume options')
    args = parser.parse_args(argv)
    forwarded = args.pipeline_args[1:] if args.pipeline_args[:1] == ['--'] else args.pipeline_args
    if any(a.split('=', 1)[0] in {'--output', '--timings', '--stage', '--run-id'} for a in forwarded):
        parser.error('Output, timings and crawl stage are managed by this wrapper')
    timings = args.results.resolve() / 'timings.jsonl'
    run_id = str(uuid4())
    if args.demo:
        if forwarded:
            parser.error('Demo has its own synthetic inputs; omit pipeline arguments')
        demo = Path(__file__).resolve().parents[4] / 'scripts/demo_ms1.py'
        command = [sys.executable, str(demo), '--output', str(args.output.resolve()), '--timings', str(timings),
                   '--run-id', run_id]
    else:
        command = [sys.executable, '-m', 'arxiv_pipeline.main', '--output', str(args.output.resolve()),
                   '--stage', 'crawl', '--timings', str(timings), '--run-id', run_id, *forwarded]
    report = measure(command, args.output, args.results, interval=args.interval,
                     label='synthetic_demo' if args.demo else 'live_unverified')
    report['run_id'] = run_id
    try:
        report['snapshot'] = current_snapshot(args.output.resolve(), run_id)
        from .statistics import summarize
        summary = summarize(args.output)
        summary.update(label=report['label'], run_id=run_id)
        atomic_json(args.results / 'statistics.json', summary)
        report['statistics_status'] = 'current_run'
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        report['statistics_status'] = 'unavailable_current_run'
        atomic_json(args.results / 'statistics_error.json', {'error': str(exc), 'run_id': run_id})
    atomic_json(args.results / 'benchmark.json', report)
    print(f"Benchmark saved to {args.results}; pipeline exit={report['exit_code']}")
    return report['exit_code'] or (1 if report['statistics_status'] != 'current_run' else 0)


if __name__ == '__main__':
    raise SystemExit(main())
