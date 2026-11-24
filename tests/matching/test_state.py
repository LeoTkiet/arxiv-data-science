import pytest


def test_atomic_matching_state_retries_short_file_locks(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import state
    calls = []
    original = state._write
    def busy(path, value):
        calls.append(path)
        if len(calls) < 3: raise PermissionError('temporary file lock')
        return original(path, value)
    monkeypatch.setattr(state, '_write', busy)
    monkeypatch.setattr(state.time, 'sleep', lambda _: None)
    state.atomic_json(tmp_path / 'run.json', {'complete': False})
    assert len(calls) == 3 and (tmp_path / 'run.json').is_file()


def test_permanent_permission_error_is_bounded(tmp_path, monkeypatch):
    from arxiv_pipeline.matching import state
    calls = []
    def denied(path, value):
        calls.append(path)
        raise PermissionError('denied')
    monkeypatch.setattr(state, '_write', denied)
    monkeypatch.setattr(state.time, 'sleep', lambda _: None)
    with pytest.raises(PermissionError): state.atomic_json(tmp_path / 'run.json', {})
    assert len(calls) == 6
