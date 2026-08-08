"""UI-neutral background workers used by the native Windows app."""

import itertools
import math
import multiprocessing as mp
import queue
import threading
import time

import numpy as np
from faster_whisper import WhisperModel

from audio_utils import SAMPLE_RATE
from codex_app_server import CodexAppServerClient
from transcription import transcribe_pcm


def _preview_process(connection, model_name, language, cpu_threads):
    try:
        started = time.perf_counter()
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
        )
        segments, _ = model.transcribe(
            np.zeros(SAMPLE_RATE, dtype=np.float32),
            language=language,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        list(segments)
        connection.send(("ready", {"startup_seconds": time.perf_counter() - started}))
        while True:
            job = connection.recv()
            if job is None:
                return
            job_id, pcm = job
            item_started = time.perf_counter()
            try:
                text = transcribe_pcm(model, pcm, language)
                connection.send((
                    "result",
                    job_id,
                    text,
                    None,
                    time.perf_counter() - item_started,
                ))
            except Exception as error:
                connection.send(("result", job_id, None, str(error), 0.0))
    except Exception as error:
        try:
            connection.send(("ready", None, str(error)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class PreviewWhisperWorker:
    def __init__(self, model_name, language, on_ready, cpu_threads=4):
        self.model_name = model_name
        self.language = language
        self.on_ready = on_ready
        self.cpu_threads = cpu_threads
        self.context = mp.get_context("spawn")
        self.lock = threading.Lock()
        self.generation = 0
        self.sequence = itertools.count()
        self.process = None
        self.connection = None
        self.callbacks = {}
        self.ready = False
        self.stopped = False

    def start(self):
        with self.lock:
            if self.stopped or (self.process is not None and self.process.is_alive()):
                return False
            self.generation += 1
            generation = self.generation
            parent, child = self.context.Pipe()
            process = self.context.Process(
                target=_preview_process,
                args=(
                    child,
                    self.model_name,
                    self.language,
                    self.cpu_threads,
                ),
                daemon=True,
            )
            process.start()
            child.close()
            self.process = process
            self.connection = parent
            self.ready = False
        threading.Thread(
            target=self._listen,
            args=(generation, process, parent),
            daemon=True,
        ).start()
        return True

    def submit(self, pcm, callback):
        with self.lock:
            if not self.ready or self.connection is None or self.stopped:
                return False
            job_id = next(self.sequence)
            self.callbacks[job_id] = callback
            connection = self.connection
        try:
            connection.send((job_id, pcm))
            return True
        except (BrokenPipeError, EOFError, OSError):
            with self.lock:
                self.callbacks.pop(job_id, None)
            return False

    def cancel(self):
        started = time.perf_counter()
        with self.lock:
            self.generation += 1
            process = self.process
            connection = self.connection
            self.process = None
            self.connection = None
            self.callbacks.clear()
            self.ready = False
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        if connection is not None:
            connection.close()
        return time.perf_counter() - started

    def stop(self):
        self.stopped = True
        return self.cancel()

    def _listen(self, generation, process, connection):
        while True:
            with self.lock:
                if generation != self.generation or self.stopped:
                    return
            try:
                if not connection.poll(0.1):
                    if process.is_alive():
                        continue
                    return
                message = connection.recv()
            except (EOFError, OSError):
                return
            if message[0] == "ready":
                if len(message) == 2:
                    _, result = message
                    error = None
                else:
                    _, result, error = message
                with self.lock:
                    if generation != self.generation:
                        return
                    self.ready = error is None
                self.on_ready(result, error)
                continue
            _, job_id, text, error, elapsed = message
            with self.lock:
                callback = self.callbacks.pop(job_id, None)
            if callback is not None:
                callback(text, error, elapsed)


class WhisperWorker:
    def __init__(self, model_name, language, on_ready, cpu_threads=8):
        self.model_name = model_name
        self.language = language
        self.on_ready = on_ready
        self.cpu_threads = cpu_threads
        self.jobs = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.accepting = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, priority, processor, callback):
        if self.accepting:
            self.jobs.put((priority, next(self.sequence), processor, callback))

    def stop(self):
        self.accepting = False
        self.jobs.put((math.inf, next(self.sequence), None, None))
        self.thread.join()

    def _run(self):
        started = time.perf_counter()
        try:
            model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cpu_threads,
            )
            load_seconds = time.perf_counter() - started
            warmup = time.perf_counter()
            segments, _ = model.transcribe(
                np.zeros(SAMPLE_RATE, dtype=np.float32),
                language=self.language,
                vad_filter=False,
                word_timestamps=True,
                condition_on_previous_text=False,
            )
            list(segments)
            result = {
                "load_seconds": load_seconds,
                "warmup_seconds": time.perf_counter() - warmup,
                "startup_seconds": time.perf_counter() - started,
            }
            error = None
        except Exception as caught:
            model = None
            result = None
            error = caught
        self.on_ready(result, error)
        while True:
            _, _, processor, callback = self.jobs.get()
            if processor is None:
                return
            try:
                result = processor(model)
                error = None
            except Exception as caught:
                result = None
                error = caught
            callback(result, error)


class CodexWorker:
    def __init__(
        self,
        model,
        effort,
        cwd,
        developer_instructions,
        timeout_seconds,
        on_ready,
        thread_id=None,
    ):
        self.jobs = queue.Queue()
        self.accepting = True
        self.client = CodexAppServerClient(
            model=model,
            effort=effort,
            cwd=cwd,
            developer_instructions=developer_instructions,
            timeout_seconds=timeout_seconds,
        )
        self.thread_id = thread_id
        self.on_ready = on_ready
        self.turn_active = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, prompt, callback, on_delta=None):
        if self.accepting:
            self.jobs.put((prompt, callback, on_delta))

    def stop(self):
        self.accepting = False
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self.jobs.put(None)
        if self.turn_active.is_set():
            self.client.request_interrupt()
        self.thread.join(timeout=3)
        self.client.stop()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self):
        try:
            ready = self.client.start(
                thread_id=self.thread_id,
                ephemeral=self.thread_id is None,
            )
            startup_error = None
        except Exception as error:
            ready = None
            startup_error = error
        self.on_ready(ready, startup_error)
        while True:
            job = self.jobs.get()
            if job is None:
                return
            prompt, callback, on_delta = job
            if startup_error is not None:
                callback(None, startup_error)
                continue
            try:
                self.turn_active.set()
                result = self.client.run_turn(prompt, on_delta=on_delta)
                error = None
            except Exception as caught:
                result = None
                error = caught
            finally:
                self.turn_active.clear()
            callback(result, error)
