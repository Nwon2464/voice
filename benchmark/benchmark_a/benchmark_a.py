#!/usr/bin/env python3
"""Automate Benchmark A through the existing Performance Test UI and pipeline."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import yaml


BENCHMARK_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCHMARK_DIR.parents[1]
APP_PATH = REPO_ROOT / "interview_app.py"
PYTHON_PATH = REPO_ROOT / ".venv" / "bin" / "python"
TEST_RUNS_DIR = REPO_ROOT / "test_runs"
APP_CONFIG_DIRNAME = "interview-assistant"
if str(REPO_ROOT) not in sys.path:
    # The shell entrypoint executes this file from benchmark/benchmark_a.
    # Make app modules importable without requiring callers to set PYTHONPATH.
    sys.path.insert(0, str(REPO_ROOT))
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
FAILURE_EVENTS = {
    "audio_error",
    "moonshine_error",
    "moonshine_startup_error",
    "question_error",
    "codex_error",
    "codex_app_server_error",
    "codex_recovery_started",
    "codex_recovery_failed",
    "codex_stale_stream_ignored",
    "codex_superseded_finished",
    "f8_ignored",
    "question_duplicate_suppressed",
    "codex_request_skipped",
}


class BenchmarkError(RuntimeError):
    """A failed or unsafe benchmark run."""


@dataclass(frozen=True)
class AudioPlan:
    warmup: Path
    questions: tuple[Path, ...]


@dataclass(frozen=True)
class Configuration:
    name: str
    model: str
    reasoning: str
    runs: int
    fast_mode: bool
    stt_language: str
    audio: AudioPlan
    contexts: tuple[Path, ...]


@dataclass(frozen=True)
class BenchmarkPlan:
    name: str
    configurations: tuple[Configuration, ...]
    execution_sequence: tuple[tuple[Configuration, int], ...]
    startup_timeout: float
    response_timeout: float


def load_yaml(path):
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise BenchmarkError(f"Cannot read YAML {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BenchmarkError("Benchmark YAML root must be a mapping")
    return payload


def resolve_file(base, value, field):
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(f"{field} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise BenchmarkError(f"{field} does not exist: {path}")
    return path


def read_audio(base, payload):
    if not isinstance(payload, dict):
        raise BenchmarkError("audio must be a mapping")
    directory = payload.get("directory", ".")
    if not isinstance(directory, str):
        raise BenchmarkError("audio.directory must be a path string")
    audio_base = Path(directory)
    if not audio_base.is_absolute():
        audio_base = (base / audio_base).resolve()
    if not audio_base.is_dir():
        raise BenchmarkError(f"audio.directory does not exist: {audio_base}")
    warmup = resolve_file(audio_base, payload.get("warmup"), "audio.warmup")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise BenchmarkError("audio.questions must be a non-empty list")
    return AudioPlan(
        warmup=warmup,
        questions=tuple(
            resolve_file(audio_base, item, "audio.questions item")
            for item in questions
        ),
    )


def read_contexts(base, values, field):
    if values is None:
        return ()
    if not isinstance(values, list):
        raise BenchmarkError(f"{field} must be a list of Markdown paths")
    paths = tuple(resolve_file(base, value, field) for value in values)
    names = [path.name.casefold() for path in paths]
    if len(set(names)) != len(names):
        raise BenchmarkError(f"{field} contains duplicate filenames")
    return paths


def configuration_audio(base, shared_audio, item):
    override = item.get("audio")
    return shared_audio if override is None else read_audio(base, override)


def parse_plan(yaml_path):
    base = yaml_path.parent.resolve()
    payload = load_yaml(yaml_path)
    name = payload.get("benchmark_name")
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise BenchmarkError(
            "benchmark_name must use letters, digits, '_' or '-'"
        )
    shared_audio = read_audio(base, payload.get("audio"))
    shared_contexts = read_contexts(base, payload.get("contexts"), "contexts")
    timeout_payload = payload.get("timeouts", {})
    if not isinstance(timeout_payload, dict):
        raise BenchmarkError("timeouts must be a mapping")
    startup_timeout = float(timeout_payload.get("startup_seconds", 90))
    response_timeout = float(timeout_payload.get("response_seconds", 90))
    if startup_timeout <= 0 or response_timeout <= 0:
        raise BenchmarkError("timeouts must be positive")
    items = payload.get("configurations")
    if not isinstance(items, list) or not items:
        raise BenchmarkError("configurations must be a non-empty list")
    configurations = []
    seen_names = set()
    for item in items:
        if not isinstance(item, dict):
            raise BenchmarkError("each configuration must be a mapping")
        config_name = item.get("name")
        if not isinstance(config_name, str) or not SAFE_NAME.fullmatch(config_name):
            raise BenchmarkError(
                "configuration.name must use letters, digits, '_' or '-'"
            )
        if config_name in seen_names:
            raise BenchmarkError(f"duplicate configuration name: {config_name}")
        seen_names.add(config_name)
        model = item.get("model")
        reasoning = item.get("reasoning")
        runs = item.get("runs")
        if not isinstance(model, str) or not model:
            raise BenchmarkError(f"{config_name}.model must be non-empty")
        if not isinstance(reasoning, str) or not reasoning:
            raise BenchmarkError(f"{config_name}.reasoning must be non-empty")
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
            raise BenchmarkError(f"{config_name}.runs must be a positive integer")
        fast_mode = item.get("fast_mode", False)
        if not isinstance(fast_mode, bool):
            raise BenchmarkError(f"{config_name}.fast_mode must be true or false")
        language = item.get("stt_language", "en")
        if language not in {"en", "ja"}:
            raise BenchmarkError(f"{config_name}.stt_language must be en or ja")
        contexts = (
            read_contexts(base, item["contexts"], f"{config_name}.contexts")
            if "contexts" in item else shared_contexts
        )
        configurations.append(Configuration(
            name=config_name,
            model=model,
            reasoning=reasoning,
            runs=runs,
            fast_mode=fast_mode,
            stt_language=language,
            audio=configuration_audio(base, shared_audio, item),
            contexts=contexts,
        ))
    configurations = tuple(configurations)
    by_name = {config.name: config for config in configurations}
    sequence_values = payload.get("execution_sequence")
    if sequence_values is None:
        execution_sequence = tuple(
            (config, run_number)
            for config in configurations
            for run_number in range(1, config.runs + 1)
        )
    else:
        if not isinstance(sequence_values, list) or not sequence_values:
            raise BenchmarkError("execution_sequence must be a non-empty list")
        run_counts = {config.name: 0 for config in configurations}
        sequence = []
        for position, config_name in enumerate(sequence_values, start=1):
            if not isinstance(config_name, str) or config_name not in by_name:
                raise BenchmarkError(
                    f"execution_sequence[{position}] must name a configuration"
                )
            config = by_name[config_name]
            run_counts[config_name] += 1
            sequence.append((config, run_counts[config_name]))
        for config in configurations:
            if run_counts[config.name] != config.runs:
                raise BenchmarkError(
                    f"execution_sequence must contain {config.name} exactly "
                    f"{config.runs} times (got {run_counts[config.name]})"
                )
        execution_sequence = tuple(sequence)
    return BenchmarkPlan(
        name=name,
        configurations=configurations,
        execution_sequence=execution_sequence,
        startup_timeout=startup_timeout,
        response_timeout=response_timeout,
    )


def read_events(log_path):
    events = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def session_logs():
    if not TEST_RUNS_DIR.is_dir():
        return []
    return list(TEST_RUNS_DIR.glob("app_session_benchmark_a_*/session.jsonl"))


def label_for(plan, config, run_number):
    return f"benchmark_{plan.name}_{config.name}_r{run_number}"


def is_completed_run(events, label, config, question_count):
    starts = [event for event in events if event.get("event") == "app_session_start"]
    if len(starts) != 1 or starts[0].get("test_label") != label:
        return False
    start = starts[0]
    if any((
        start.get("codex_model") != config.model,
        start.get("codex_reasoning_effort") != config.reasoning,
        start.get("codex_fast_mode") is not config.fast_mode,
        start.get("language") != config.stt_language,
    )):
        return False
    if any(event.get("event") in FAILURE_EVENTS for event in events):
        return False
    questions = [event for event in events if event.get("event") == "question"]
    responses = [event for event in events if event.get("event") == "codex_response"]
    wav_starts = [
        event for event in events
        if event.get("event") == "benchmark_wav_start"
    ]
    if len(questions) != question_count or len(responses) != question_count:
        return False
    if len(wav_starts) != question_count:
        return False
    if [event.get("question") for event in questions] != list(
        range(1, question_count + 1)
    ):
        return False
    if [event.get("question") for event in responses] != list(
        range(1, question_count + 1)
    ):
        return False
    if any(event.get("commit_source") != "f8" for event in questions):
        return False
    if any(event.get("first_visible_seconds") is None for event in responses):
        return False
    if any(not event.get("cursor_complete") for event in questions):
        return False
    if any(event.get("audio_drop_samples") != 0 for event in questions):
        return False
    ends = [event for event in events if event.get("event") == "app_session_end"]
    return len(ends) == 1 and (
        ends[0].get("questions") == question_count
        and ends[0].get("codex_requests") == question_count
        and ends[0].get("cleanup_errors") == []
    )


def completed_labels(plan, config):
    labels = set()
    count = len(config.audio.questions) + 1
    for log_path in session_logs():
        events = read_events(log_path)
        for run_number in range(1, config.runs + 1):
            label = label_for(plan, config, run_number)
            if is_completed_run(events, label, config, count):
                labels.add(label)
    return labels


def app_is_running():
    result = subprocess.run(
        ["pgrep", "-f", str(APP_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def wait_until(predicate, timeout, message, on_poll=None):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        try:
            value = predicate()
        except Exception as error:  # transient UI/log startup states
            last_error = error
            value = None
        if value:
            return value
        time.sleep(0.05)
    suffix = f" ({last_error})" if last_error else ""
    raise BenchmarkError(f"Timed out waiting for {message}{suffix}")


def event_count(log_path, event_name, question=None):
    events = read_events(log_path)
    return sum(
        event.get("event") == event_name
        and (question is None or event.get("question") == question)
        for event in events
    )


def assert_start_configuration(log_path, config):
    events = read_events(log_path)
    starts = [event for event in events if event.get("event") == "app_session_start"]
    if len(starts) != 1:
        raise BenchmarkError("Performance log has no unique app_session_start")
    start = starts[0]
    actual = {
        "model": start.get("codex_model"),
        "reasoning": start.get("codex_reasoning_effort"),
        "fast_mode": start.get("codex_fast_mode"),
        "stt_language": start.get("language"),
    }
    expected = {
        "model": config.model,
        "reasoning": config.reasoning,
        "fast_mode": config.fast_mode,
        "stt_language": config.stt_language,
    }
    if actual != expected:
        raise BenchmarkError(
            f"New session settings were not applied: expected {expected}, got {actual}"
        )


def wait_response(log_path, question, timeout, pump):
    wait_until(
        lambda: event_count(log_path, "codex_response", question) == 1,
        timeout,
        f"Codex response for question {question}",
        on_poll=pump,
    )


def play_and_commit(app, wav_path, question_number, log_path, timeout, pump):
    started_at = time.time_ns()
    app.record_benchmark_wav_start(wav_path.name, started_at)
    print(f"  [{question_number:02d}] playback {wav_path.name}", flush=True)
    process = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(wav_path)],
        cwd=REPO_ROOT,
    )
    while process.poll() is None:
        pump()
        time.sleep(0.01)
    if process.returncode != 0:
        raise BenchmarkError(f"WAV playback failed: {wav_path}")
    print(f"  [{question_number:02d}] F8 commit", flush=True)
    app.trigger_f8()
    wait_response(log_path, question_number, timeout, pump)
    response = next(
        event for event in reversed(read_events(log_path))
        if event.get("event") == "codex_response"
        and event.get("question") == question_number
    )
    print(
        f"  [{question_number:02d}] first visible "
        f"{response['first_visible_seconds']:.3f}s; response complete",
        flush=True,
    )


def prepare_thread(config_home, config):
    """Use the existing Context Sync backend without opening a GTK dialog."""
    import interview_app
    from context_manager import ContextManager
    from interview_thread_backend import InterviewThreadBackend
    from session_store import SessionStore

    config_dir = config_home / APP_CONFIG_DIRNAME
    store = SessionStore(config_dir / "sessions.json")
    contexts = ContextManager(config_dir)
    settings = {
        "codex_model": config.model,
        "codex_reasoning_effort": config.reasoning,
        "codex_fast_mode": config.fast_mode,
        "stt_language": config.stt_language,
    }
    session = store.create("Benchmark A", settings=settings)
    target = contexts.ensure_session(session["session_id"])
    for source in config.contexts:
        shutil.copy2(source, target / source.name)
    backend = InterviewThreadBackend(
        store,
        contexts,
        lambda selected: interview_app._new_codex_client(selected),
    )
    result = backend.create(session)
    return result["interview_thread_id"], settings


def run_once(plan, config, run_number):
    label = label_for(plan, config, run_number)
    if app_is_running():
        raise BenchmarkError(
            "Interview App is already running; close it before starting Benchmark A"
        )
    with tempfile.TemporaryDirectory(prefix="benchmark-a-") as temporary_dir:
        config_home = Path(temporary_dir)
        import interview_app
        from gi.repository import GLib

        def pump():
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)

        print(f"[{label}] creating isolated session and Codex thread", flush=True)
        try:
            thread_id, settings = prepare_thread(config_home, config)
            print(
                f"[{label}] model={config.model} reasoning={config.reasoning} "
                f"fast_mode={config.fast_mode} stt={config.stt_language}",
                flush=True,
            )
            app = interview_app.HeadlessInterviewApp(thread_id, settings, label)
            log_path = app.log_path
            assert_start_configuration(log_path, config)
            wait_until(
                lambda: event_count(log_path, "moonshine_ready") == 1,
                plan.startup_timeout,
                "Moonshine readiness",
                on_poll=pump,
            )
            print(f"[{label}] Moonshine ready", flush=True)
            wait_until(
                lambda: event_count(log_path, "codex_app_server_ready") == 1,
                plan.startup_timeout,
                "Codex thread readiness",
                on_poll=pump,
            )
            print(f"[{label}] Codex thread ready", flush=True)
            print(f"[{label}] warmup (excluded from benchmark questions)", flush=True)
            play_and_commit(
                app, config.audio.warmup, 1, log_path,
                plan.response_timeout, pump,
            )
            for index, wav_path in enumerate(config.audio.questions, start=2):
                play_and_commit(
                    app, wav_path, index, log_path,
                    plan.response_timeout, pump,
                )
            app.shutdown()
            events = read_events(log_path)
            if not is_completed_run(
                events, label, config, len(config.audio.questions) + 1
            ):
                raise BenchmarkError(f"Run did not meet normal-run checks: {log_path}")
            print(f"completed {label}: {log_path}")
        finally:
            if "app" in locals():
                app.shutdown()


def run(plan):
    if not APP_PATH.is_file() or not PYTHON_PATH.is_file():
        raise BenchmarkError("Run from an Interview Assistant checkout with .venv")
    completed_by_config = {
        config.name: completed_labels(plan, config)
        for config in plan.configurations
    }
    for config, run_number in plan.execution_sequence:
        label = label_for(plan, config, run_number)
        if label in completed_by_config[config.name]:
            print(f"already completed {label}; skipping")
            continue
        print(f"starting {label}")
        run_once(plan, config, run_number)


def main():
    parser = argparse.ArgumentParser(
        description="Run Benchmark A through the real Performance Test pipeline."
    )
    parser.add_argument("yaml_path", type=Path, help="Benchmark YAML file")
    arguments = parser.parse_args()
    try:
        plan = parse_plan(arguments.yaml_path.resolve())
        run(plan)
    except BenchmarkError as error:
        print(f"Benchmark A stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
