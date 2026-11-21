"""Offline matching CLI. Synthetic adapters require an explicit smoke/fixture flag."""
import argparse
import json
from pathlib import Path

from arxiv_pipeline.matching.pipeline import run_matching


def main(argv=None):
    parser = argparse.ArgumentParser(prog='matching', description='Milestone 3 offline orchestration')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('run', 'smoke'):
        p = commands.add_parser(command)
        p.add_argument('--output', required=True, type=Path)
        p.add_argument('--seed', type=int)
        group = p.add_mutually_exclusive_group()
        group.add_argument('--resume', action='store_true')
        group.add_argument('--force', action='store_true')
        if command == 'run':
            p.add_argument('--input', required=True, type=Path, help='JSON config with snapshot roots and publication IDs')
            p.add_argument('--labels', type=Path)
            p.add_argument('--partitions', type=Path)
            p.add_argument('--population-count', type=int)
            p.add_argument('--auto-label', action='store_true')
            p.add_argument('--fixture', action='store_true', help='Explicitly use synthetic business adapters; never experimental results')
    args = parser.parse_args(argv)
    adapters = None
    try:
        if args.command == 'smoke':
            from arxiv_pipeline.matching.smoke import prepare_fixture, fixture_adapters
            output = args.output.resolve()
            config = prepare_fixture(output.parent / (output.name + '-fixture-input'))
            adapters = fixture_adapters()
        else:
            config = json.loads(args.input.read_text(encoding='utf-8-sig'))
            if not isinstance(config, dict): raise ValueError('Input config must be a JSON object')
            if config.get('fixture_mode') and not args.fixture:
                parser.error('Synthetic config requires --fixture (or use matching smoke)')
            for name in ('ms1_root', 'ms2_root', 'labels', 'partitions'):
                if config.get(name):
                    config[name] = (args.input.resolve().parent / config[name]).resolve()
            for name in ('labels', 'partitions', 'population_count'):
                value = getattr(args, name)
                if value is not None: config[name] = value
            if args.auto_label: config['auto_label'] = True
            if args.fixture:
                from arxiv_pipeline.matching.smoke import fixture_adapters
                config['fixture_mode'] = True
                adapters = fixture_adapters()
        config.update(output=args.output, resume=args.resume, force=args.force)
        if args.seed is not None: config['seed'] = args.seed
        result = run_matching(config, adapters=adapters)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
