"""Reissue cache certificates only for unchanged producers with equal consumer inputs.

The parser and completion-index revisions are deliberately untouched: migration
must not invalidate results already produced by the repaired runtime.
"""

import inspect
import hashlib
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from arxiv_pipeline.common.schemas import PARSING_SCHEMA_VERSION
from arxiv_pipeline.common.utils import atomic_json, paper_key, utc_now
from arxiv_pipeline.pipeline import parse


def _checkpoint(root, stage, paper, number):
    return parse._path(root, f"state/stages/{stage}/{paper_key(paper)}/{number}.json")


def _metadata(path):
    return path.with_name(path.stem + ".dependencies.json")


def _fingerprint(runner, stage, upstream, *, implementation=None, pipeline=None):
    material = dict(upstream)
    material["context"] = {k: v for k, v in upstream["context"].items() if k != "input_root"}
    return parse._digest(
        dict(
            stage=stage,
            schema=PARSING_SCHEMA_VERSION,
            input=material,
            implementation=implementation or runner.revisions[stage],
            pipeline=pipeline or runner.pipeline_revision,
        )
    )


def _valid_current(runner, stage, record, version, upstream):
    path = _checkpoint(runner.output, stage, record["arxiv_id"], version["version"])
    result, meta = parse._read(path, cache=True), parse._read(_metadata(path), cache=True)
    if (
        not isinstance(result, dict)
        or not isinstance(meta, dict)
        or result.get("status") not in parse.TERMINAL
    ):
        return False
    try:
        return (
            meta.get("fingerprint") == _fingerprint(runner, stage, upstream)
            and meta.get("checkpoint_hash") == parse._digest(result)
            and meta.get("artifacts") == runner.validate(result, stage, record, version, upstream)
        )
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        return False


class _Evidence:
    def __init__(self, root, runner, record, version):
        self.root, self.runner, self.record, self.version = root, runner, record, version
        self.loaded = {}

    def stage(self, name):
        if name in self.loaded:
            return self.loaded[name]
        path = _checkpoint(self.root, name, self.record["arxiv_id"], self.version["version"])
        result, meta = parse._read(path, cache=True), parse._read(_metadata(path), cache=True)
        if not isinstance(result, dict) or not isinstance(meta, dict):
            raise ValueError(f"{name}: checkpoint/dependency metadata missing")
        if (
            result.get("stage"),
            result.get("paper_id"),
            result.get("version"),
            result.get("schema_version"),
        ) != (name, self.record["arxiv_id"], self.version["version"], PARSING_SCHEMA_VERSION):
            raise ValueError(f"{name}: identity/schema mismatch")
        if type(result.get("version")) is not int or result.get("status") not in parse.TERMINAL:
            raise ValueError(f"{name}: not a terminal version stage")
        if (
            meta.get("schema_version") != PARSING_SCHEMA_VERSION
            or meta.get("checkpoint_hash") != parse._digest(result)
            or not isinstance(meta.get("fingerprint"), str)
        ):
            raise ValueError(f"{name}: unauthenticated checkpoint")
        inventory, payloads = {}, {}
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, dict) or not parse.REQUIRED[name].issubset(artifacts):
            raise ValueError(f"{name}: missing artifacts")
        namespace = f'parsed/{paper_key(self.record["arxiv_id"])}/v{self.version["version"]}/'
        for label, relative in artifacts.items():
            path = parse._path(self.root, relative)
            if (
                not relative.startswith(namespace)
                or not path.is_file()
                or path.stat().st_size > 128 * 1024 * 1024
            ):
                raise ValueError(f"{name}: invalid artifact boundary/size")
            data = path.read_bytes()
            inventory[relative] = hashlib.sha256(data).hexdigest()
            payload = json.loads(data.decode("utf-8-sig"))
            if not isinstance(payload, dict) or (
                payload.get("schema_version"),
                payload.get("arxiv_id"),
                payload.get("version"),
            ) != (PARSING_SCHEMA_VERSION, self.record["arxiv_id"], self.version["version"]):
                raise ValueError(f"{name}: artifact identity mismatch")
            payloads[label] = payload
        if meta.get("artifacts") != inventory:
            raise ValueError(f"{name}: artifact hash mismatch")
        self.loaded[name] = (result, meta, payloads)
        return self.loaded[name]

    def producer(self, name, upstream):
        result, meta, payloads = self.stage(name)
        adapter = self.runner.adapters[name]
        if (
            getattr(adapter, "__module__", None) != parse.MODULES[name]
            or getattr(adapter, "__qualname__", None) != "run"
        ):
            raise ValueError(f"{name}: custom producers cannot be migrated")
        filename = inspect.getsourcefile(adapter)
        if not filename:
            raise ValueError(f"{name}: producer source unavailable")
        source = Path(filename).read_bytes().replace(b"\r\n", b"\n")
        implementations = {self.runner.revisions[name]}
        for data in (source, source.replace(b"\n", b"\r\n")):
            implementations.add(
                parse._digest(
                    dict(
                        module=adapter.__module__,
                        name=adapter.__qualname__,
                        source=hashlib.sha256(data).hexdigest(),
                        revision=str(getattr(adapter, "__parsing_revision__", "")),
                    )
                )
            )
        pipeline_bytes = Path(parse.__file__).read_bytes().replace(b"\r\n", b"\n")
        pipelines = {
            self.runner.pipeline_revision,
            *parse._COMPATIBLE_PIPELINE_REVISIONS,
            hashlib.sha256(pipeline_bytes).hexdigest(),
            hashlib.sha256(pipeline_bytes.replace(b"\n", b"\r\n")).hexdigest(),
        }
        for implementation in implementations:
            for pipeline in pipelines:
                if meta["fingerprint"] == _fingerprint(
                    self.runner, name, upstream, implementation=implementation, pipeline=pipeline
                ):
                    return (
                        result,
                        meta,
                        payloads,
                        dict(implementation=implementation, pipeline=pipeline),
                    )
        raise ValueError(f"{name}: producer/dependency fingerprint not compatible")


def _hierarchy_input(result, gathered):
    projection = {
        k: gathered.get(k)
        for k in ("schema_version", "arxiv_id", "version", "is_empty_source", "merged_body_tex")
    }
    projection.update(status=result["status"], warnings=gathered.get("warnings", []))
    if gathered.get("is_empty_source"):
        projection["main_tex_path"] = gathered.get("main_tex_path")
    return projection


def _adopt(evidence, stage, upstream, old_upstream, projection):
    result, meta, payloads, producer = evidence.producer(stage, old_upstream)
    runner = evidence.runner
    for relative in result["artifacts"].values():
        original = parse._path(evidence.root, relative)
        target = parse._path(runner.output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + uuid4().hex + ".partial")
        try:
            temporary.write_bytes(original.read_bytes())
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    inventory = runner.validate(result, stage, evidence.record, evidence.version, upstream)
    if inventory != meta["artifacts"]:
        raise ValueError("Legacy artifact changed while copying")
    path = _checkpoint(
        runner.output, stage, evidence.record["arxiv_id"], evidence.version["version"]
    )
    proof = dict(
        format="ms2-stage-migration-1",
        migrated_at=utc_now(),
        strategy="equal-consumer-input",
        legacy_checkpoint_hash=meta["checkpoint_hash"],
        legacy_fingerprint=meta["fingerprint"],
        legacy_artifacts=inventory,
        producer=producer,
        consumer_input_hash=parse._digest(projection),
    )
    atomic_json(path, result)
    atomic_json(
        _metadata(path),
        dict(
            schema_version="2.0",
            fingerprint=_fingerprint(runner, stage, upstream),
            checkpoint_hash=parse._digest(result),
            artifacts=inventory,
            migration=proof,
        ),
    )
    return result


def pending_versions(source, output, records):
    """Find versions needing tree evidence before touching the legacy Drive root."""
    runner = parse._Runner(output, True, None, None)
    pending = set()
    for record, _, conflict in records:
        if conflict or record.get("status") != "success":
            continue
        try:
            versions = parse._versions(record)
        except (ValueError, TypeError, KeyError):
            continue
        for version in versions:
            try:
                snapshot, stages = parse._snapshot(source, record, version)
                context = dict(input_root=str(source), input_snapshot_id=snapshot)
                upstream = dict(context=context, stages=stages)
                if not _valid_current(runner, "resolve", record, version, upstream):
                    raise ValueError("Current resolve needs processing")
                resolved = parse._read(
                    _checkpoint(output, "resolve", record["arxiv_id"], version["version"])
                )
                upstream = dict(context=context, stages={"resolve": resolved})
                if not _valid_current(runner, "hierarchy", record, version, upstream):
                    raise ValueError("Current hierarchy needs processing")
                hierarchy = parse._read(
                    _checkpoint(output, "hierarchy", record["arxiv_id"], version["version"])
                )
                upstream = dict(context=context, stages={"hierarchy": hierarchy})
                if not _valid_current(runner, "normalize", record, version, upstream):
                    raise ValueError("Current normalization needs processing")
            except (ValueError, TypeError, KeyError, OSError, AttributeError):
                pending.add((record["arxiv_id"], version["version"]))
    return pending


def prepare_batch(source, output, legacy, records):
    """Prepare reusable trees locally; resolve current inputs before any semantic reuse.

    Return stage events so the caller does not count preparation work as cache hits.
    The normal parse.run remains responsible for bibliography/export/current QA.
    """
    source, output = parse.validate_roots(source, output)
    legacy = Path(legacy).resolve()
    for boundary in (source, output):
        if legacy.is_relative_to(boundary) or boundary.is_relative_to(legacy):
            raise ValueError("Legacy MS2 input must be disjoint from current input/output")
    if not legacy.is_dir():
        raise ValueError("Legacy MS2 input is not a directory")
    events = []
    decisions = []
    runner = parse._Runner(output, True, None, events.append)
    for record, _, conflict in records:
        if conflict or record.get("status") != "success":
            continue
        try:
            versions = parse._versions(record)
        except (ValueError, TypeError, KeyError):
            continue
        for version in versions:
            paper, number = record["arxiv_id"], version["version"]
            if not any(
                _checkpoint(legacy, s, paper, number).is_file() for s in ("hierarchy", "normalize")
            ):
                continue
            evidence = _Evidence(legacy, runner, record, version)
            try:
                snapshot, stages = parse._snapshot(source, record, version)
                context = dict(input_root=str(source), input_snapshot_id=snapshot)
                blocked = (
                    "MS1 checkpoint unavailable"
                    if any(stages.get(s) is None for s in ("download", "extract"))
                    else None
                )
                resolved = runner.stage(
                    "resolve",
                    record,
                    version,
                    dict(context=context, stages=stages),
                    blocked=blocked,
                )
                if resolved["status"] not in parse.TERMINAL:
                    raise ValueError("Current resolver did not complete: " + resolved["status"])
                current_gathered = parse._read(
                    parse._path(output, resolved["artifacts"]["gathered"])
                )
            except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                decisions.extend(
                    dict(
                        paper_id=paper,
                        version=number,
                        stage=stage,
                        action="recompute",
                        reason=str(exc),
                    )
                    for stage in ("hierarchy", "normalize")
                )
                continue
            hierarchy_up = dict(context=context, stages={"resolve": resolved})
            hierarchy = None
            for stage in ("hierarchy", "normalize"):
                started = perf_counter()
                upstream = (
                    hierarchy_up
                    if stage == "hierarchy"
                    else dict(context=context, stages={"hierarchy": hierarchy})
                )
                decision = dict(
                    paper_id=paper, version=number, stage=stage, action="recompute", reason=None
                )
                try:
                    if stage == "normalize" and (
                        hierarchy is None or hierarchy["status"] not in parse.TERMINAL
                    ):
                        raise ValueError("Current hierarchy is unavailable")
                    if _valid_current(runner, stage, record, version, upstream):
                        decision.update(
                            action="current_cache", reason="Current checkpoint already validates"
                        )
                    else:
                        old_resolve, _, old_payloads = evidence.stage("resolve")
                        old_gathered = old_payloads["gathered"]
                        old_snapshot = old_gathered.get("input_snapshot_id")
                        if not isinstance(old_snapshot, str) or not old_snapshot:
                            raise ValueError("Old input snapshot missing")
                        old_context = dict(input_snapshot_id=old_snapshot)
                        if stage == "hierarchy":
                            old_input = _hierarchy_input(old_resolve, old_gathered)
                            projection = _hierarchy_input(resolved, current_gathered)
                            old_up = dict(context=old_context, stages={"resolve": old_resolve})
                        else:
                            old_hierarchy, _, payloads = evidence.stage("hierarchy")
                            old_input = payloads["raw_tree"]
                            projection = parse._read(
                                parse._path(output, hierarchy["artifacts"]["raw_tree"])
                            )
                            old_up = dict(context=old_context, stages={"hierarchy": old_hierarchy})
                        if parse._digest(old_input) != parse._digest(projection):
                            raise ValueError("Consumer input changed")
                        adopted = _adopt(evidence, stage, upstream, old_up, projection)
                        decision.update(
                            action="reused",
                            reason="Producer fingerprint and consumer inputs verified",
                        )
                        events.append(
                            dict(
                                kind="stage",
                                stage=stage,
                                paper_id=paper,
                                version=number,
                                status=adopted["status"],
                                cached=True,
                                migrated=True,
                                elapsed_seconds=perf_counter() - started,
                            )
                        )
                except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                    decision["reason"] = str(exc)
                decisions.append(decision)
                if stage == "hierarchy":
                    hierarchy = runner.stage(
                        stage,
                        record,
                        version,
                        upstream,
                        blocked=parse._blocked({"resolve": resolved}),
                    )
    return dict(format="ms2-stage-migration-report-1", decisions=decisions, events=events)


def merge_events(prepared, current):
    """Count each stage once; preparation executions are not cache hits."""
    groups = {}
    for event in [*prepared, *current]:
        key = (event["paper_id"], event["version"], event["stage"])
        groups.setdefault(key, []).append(event)
    result = []
    for events in groups.values():
        row = dict(
            events[-1],
            cached=all(e["cached"] for e in events),
            elapsed_seconds=sum(e["elapsed_seconds"] for e in events),
        )
        if any(e.get("migrated") for e in events):
            row["migrated"] = row["cached"]
        result.append(row)
    return result
