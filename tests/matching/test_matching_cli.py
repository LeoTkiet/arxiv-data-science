import json

import pytest


def test_matching_smoke_cli_and_resume(tmp_path, capsys):
    from arxiv_pipeline.main import main
    out = tmp_path / 'result'
    assert main(['matching', 'smoke', '--output', str(out)]) == 0
    assert json.loads((out / 'manifests/run.json').read_text())['synthetic'] is True
    assert main(['matching', 'smoke', '--output', str(out), '--resume']) == 0
    receipt = json.loads((out / 'manifests/run.json').read_text())
    assert receipt['complete'] and all(s['cached'] for s in receipt['stages'])
    # Native Windows input paths must not leak into serialized relative artifacts.
    from arxiv_pipeline.reporting.milestone2.validation import safe_path
    for stage in receipt['stages']:
        for relative in stage['artifacts']:
            assert '\\' not in relative
            assert safe_path(out, relative).is_file()


def test_cli_rejects_fixture_without_explicit_flag(tmp_path):
    from arxiv_pipeline.matching.cli import main
    from tests.matching.test_io import ready_snapshot
    cfg = ready_snapshot(tmp_path / 'snapshot')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({k: str(v) if hasattr(v, '__fspath__') else v for k, v in cfg.items()}))
    with pytest.raises(SystemExit) as exc:
        main(['run', '--input', str(path), '--output', str(tmp_path / 'output')])
    assert exc.value.code == 2


def test_cli_real_mode_missing_upstream_returns_incomplete(tmp_path):
    from arxiv_pipeline.matching.cli import main
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(ms1_root='m1', ms2_root='m2', ids=['2310.12345'], population_count=5000)))
    assert main(['run', '--input', str(path), '--output', str(tmp_path / 'output')]) == 1
    report = json.loads((tmp_path / 'output/manifests/run.json').read_text())
    assert not report['complete'] and not report['synthetic']


def test_cli_missing_population_count_reports_config_error_before_work(tmp_path, capsys):
    from arxiv_pipeline.matching.cli import main
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(ms1_root='m1', ms2_root='m2', ids=['2310.12345'])))
    output = tmp_path / 'output'
    with pytest.raises(SystemExit) as exc:
        main(['run', '--input', str(path), '--output', str(output)])
    assert exc.value.code == 2
    assert 'population_count' in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize('override', [False, True])
def test_cli_population_count_from_json_or_flag(tmp_path, monkeypatch, override):
    from arxiv_pipeline.matching import cli
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(ms1_root='m1', ms2_root='m2', ids=['2310.12345'], population_count=5000)))
    captured = {}

    def run(config, **kwargs):
        captured.update(config)
        return {'complete': True}

    monkeypatch.setattr(cli, 'run_matching', run)
    argv = ['run', '--input', str(path), '--output', str(tmp_path / 'output')]
    if override:
        argv += ['--population-count', '25000']
    assert cli.main(argv) == 0
    assert captured['population_count'] == (25000 if override else 5000)


def test_cli_accepts_native_paths_relative_to_config(tmp_path, monkeypatch):
    from pathlib import Path
    from arxiv_pipeline.matching import cli
    config_file = tmp_path / 'configs/run.json'
    config_file.parent.mkdir()
    names = ('ms1_root', 'ms2_root', 'labels', 'partitions')
    # str(Path) deliberately produces backslashes on Windows.
    relative = {name: str(Path('inputs') / name) for name in names}
    config_file.write_text(json.dumps(dict(relative, ids=['2310.12345'])))
    captured = {}

    def run(config, **kwargs):
        captured.update(config)
        return {'complete': True}

    monkeypatch.setattr(cli, 'run_matching', run)
    assert cli.main(['run', '--input', str(config_file), '--output', str(tmp_path / 'output')]) == 0
    for name in names:
        assert captured[name] == (config_file.parent / 'inputs' / name).resolve()
