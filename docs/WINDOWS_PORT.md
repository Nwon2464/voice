# Windows WASAPI loopback → WSL probe

This branch does **not** port the interview application to Windows.  The
current Moonshine streaming worker, F8/F9 handling, Session/Launcher flow, and
`interview_app.py` execution path continue to run exactly as on Linux.

This narrowly scoped probe verifies one boundary only:

```text
Windows default output device
          │ WASAPI loopback (native Windows Python)
          ▼
  16 kHz / mono / signed-int16 little-endian PCM
          │ length-prefixed binary stdin/stdout frames
          ▼
    WSL audio_probe.py → wasapi_probe.wav
```

The Windows helper has no GUI, microphone capture, Whisper, Moonshine, or
Codex dependencies.  It requests the default Windows output endpoint from
SoundCard and sends `A` PCM frames and `S` UTF-8 JSON status frames.  When the
WSL client requests global hotkeys, it also sends `H` UTF-8 JSON event frames.
WSL sends `Q` on stdin to stop it.

## Setup

From the repository root in WSL, first confirm that Windows Python is available
through interop:

```bash
py.exe -3 --version
```

Create the isolated Windows helper environment:

```bash
bash ./setup_windows_bridge.sh
```

Only `numpy` and `SoundCard` are installed under
`%LOCALAPPDATA%\InterviewAssistantBridge\.venv`.  The Linux virtual environment
and its dependencies remain unchanged.

The helper pins NumPy 2.4.2 because it provides CPython 3.14 Windows wheels.
Setup uses `--only-binary=:all:`: if a compatible wheel is ever unavailable,
installation fails instead of requiring Visual Studio or attempting a source
build.  The setup script runs Windows commands from `/mnt/c`, avoiding the
separate `cmd.exe` warning caused by a WSL UNC working directory.

## Ten-second capture

Play speech through the Windows default output device, then run from WSL:

```bash
.venv/bin/python -m windows_port.audio_probe \
  --seconds 10 \
  --output wasapi_probe.wav
```

The process prints a JSON report and writes the captured PCM as a 16 kHz,
mono, 16-bit WAV.  A successful validation has all of the following:

- `ok: true`
- a `ready` status with the selected output device
- `captured_seconds` near 10
- `audio_detected: true` while speech is playing
- a nonempty `wasapi_probe.wav` that plays at normal speed and contains the
  Windows system output rather than microphone audio

The probe returns a nonzero code if the bridge errors, no PCM arrives, or the
RMS signal level remains below the detection threshold.  It still saves a WAV
when silent PCM was received, which helps distinguish silence from a transport
failure.

To choose a non-default output device, copy an `id` from the `ready` status and
run the probe again with:

```bash
export INTERVIEW_AUDIO_DEVICE_ID='SoundCard device id'
.venv/bin/python -m windows_port.audio_probe --seconds 10
```

`INTERVIEW_WINDOWS_PYTHON` can override the WSL-visible path of the native
Windows helper Python executable when the helper environment is installed in a
different location.

## Moonshine streaming probe

After the WAV probe succeeds, validate the next boundary without changing the
interview application.  This probe starts the existing
`MoonshineStreamingWorker`, forwards every bridge PCM chunk with a contiguous
sample cursor, and prints changing preview text to the terminal.

```bash
.venv/bin/python -m windows_port.moonshine_probe --language en --seconds 20
.venv/bin/python -m windows_port.moonshine_probe --language ja --seconds 20
```

Play matching English or Japanese speech through the Windows output device.
At the end, confirm `preview_text` and `transcript_detected`, along with a
nonzero `pcm_bytes_forwarded`, contiguous `received`/`queued`/`consumed`
sample cursors, and `audio_drop_samples: 0`.  This remains a standalone
validation: it does not route PCM into the interview application's worker or
connect F8/F9.

At the end of the requested capture duration, the probe first stops the bridge
and fixes its final received cursor.  It then waits up to `--drain-timeout`
(10 seconds by default) for Moonshine to consume that cursor before printing
its JSON report.  A normal report has matching received, queued, and consumed
cursors; a drain timeout is reported explicitly instead of silently dropping
the queued tail.  The report distinguishes the cursor observed at the deadline
(`drain_consumed_at_deadline_sample_cursor`) from the cursor after `worker.stop`
has joined (`final_consumed_after_shutdown_sample_cursor`).  Its `drain_status`
is one of `completed_within_timeout`,
`timed_out_but_completed_during_shutdown`, or `incomplete_after_shutdown`.
`audio_drop_samples` and `audio_loss_detected` remain separate from a drain
deadline miss.

## STEP 3B: Windows PCM + F8/F9 semantic probe

This standalone probe combines WASAPI PCM and the native global F8/F9 bridge
with the existing `MoonshineStreamingWorker`.  It has no UI, Session, launcher,
or Codex integration.

```bash
.venv/bin/python -m windows_port.semantic_probe --language en --seconds 60
.venv/bin/python -m windows_port.semantic_probe --language ja --seconds 60
```

Play a Windows-output question and press F8 at its boundary.  The probe fixes
the current received PCM cursor while the bridge callback is executing and
passes that exact cursor to `request_snapshot`.  Moonshine retains its normal
barrier, force-update, silence accumulator, and stream-reset behavior; it does
not skip the backlog or move the F8 target forward.  Later PCM belongs to later
snapshots.

F8 is the only new semantic question action.  A valid F8 increments the
semantic question number; empty/duplicate F8 does not.  After a valid F8, play
the correction/continuation and press F9.  F9 updates the same question number;
without a valid prior F8/F9 question it is rejected before a snapshot is
requested.  Per-key terminal JSON contains target/backlog-at-press, barrier and
force-update timings, accept/reject state, and committed text.  The final JSON
includes all ordered semantic events and aggregate counts.

## Deterministic WAV semantic probe

Use this after the live probes to validate F8/F9 semantics without human audio
playback or key timing. It feeds a WAV directly into the same worker in 10 ms
chunks, injects semantic F8/F9 events at exact PCM cursors, and never attempts
to automate a physical Windows keypress.

```bash
.venv/bin/python -m windows_port.semantic_file_probe --language ja --wav /path/to/japanese.wav
.venv/bin/python -m windows_port.semantic_file_probe --language en --wav /path/to/english.wav
```

The probe prints source WAV metadata. Non-16 kHz mono s16le PCM input is
resampled to a `/tmp` 16 kHz mono s16le test WAV while leaving the source file
unchanged. It records a full-audio reference snapshot, then supplies the same
file again and injects F8 immediately after the final chunk is submitted. The
F8 received/target cursor must equal the total WAV samples; the worker barrier
must consume through that target before committing. Fast file feed is labelled
`synthetic_backlog_stress_fast_10ms_feed`, so its backlog measurements are not
mixed with live realtime performance measurements.

It also searches for a low-RMS silence boundary between 50% and 70% of the WAV
to run F8→F9 continuation, and separately verifies F9-without-F8 rejection.
Reference/F8 transcripts, normalized comparison, similarity, and tail-suffix
comparison are diagnostics only: cursor/barrier invariants determine transport
correctness, while a small ASR revision difference is not treated as tail loss.

## STEP 3A: Windows global F8/F9 transport probe

This standalone probe registers F8 and F9 through native Win32
`RegisterHotKey`, then transfers every press through the same binary bridge.
It does not start WASAPI capture, Moonshine, F8/F9 semantic commits, or the
interview application.

```bash
.venv/bin/python -m windows_port.hotkey_probe --seconds 30
```

After the ready message, focus Chrome, Zoom, or another native Windows
application—not the WSL terminal—and press F8/F9.  The WSL terminal should
print each event in order:

```text
[hotkey] F8
[hotkey] F9
```

The final JSON includes `f8_count`, `f9_count`, and ordered `hotkey_events`.
Each event is an `H` binary frame containing JSON with `event: "hotkey"`,
`key`, increasing `sequence`, and `timestamp_ns`.  Existing `A` audio and `S`
status frames are unchanged.  `FrameWriter` serializes audio/status/hotkey
writes so concurrently generated frames cannot interleave on stdout.

If F8 or F9 is already registered by another application, the helper emits a
`hotkey_error` status, the probe reports it in `errors`, and exits nonzero.
Close or reconfigure the application holding the shortcut and run the probe
again.  The helper unregisters every successfully registered key during normal
shutdown and partial-registration cleanup.

## STEP 4: WSLg Interview Assistant integration

The normal Linux backend remains the default. Select the Windows bridge only
explicitly, with `INTERVIEW_AUDIO_BACKEND=windows_bridge`; supported values are
`pulseaudio` and `windows_bridge`. The Windows backend uses the existing WSLg
GTK Session → Preparation → Interview UI and the existing Moonshine semantic
path, but it does not start ffmpeg/PulseAudio capture, install GNOME keybindings,
or create the Linux trigger socket. Instead, it starts one `WindowsBridgeClient`
for WASAPI PCM and native F8/F9 events.

For the first actual-app smoke test, run the Codex-off diagnostic flow:

```bash
cd /home/won/voice
./start_wsl_windows_app.sh
```

This wrapper sets `INTERVIEW_AUDIO_BACKEND=windows_bridge` and reuses the
existing `stt_diagnostic` mode, so Session and Preparation still appear but no
Codex request is made. Choose/create a Session, continue through Preparation,
and start Interview. The runtime log (and diagnostic session JSONL) records the
selected playback device, bridge readiness/hotkey state, F8/F9 sequence and
cursor/barrier diagnostics, and final bridge cursor state at shutdown.

With a Windows native application such as Chrome focused, play audio and press
F8 at the question boundary. The Interviewer window should commit one question.
Play the continuation and press F9; it should update that same question number.
Close the app and confirm the log contains `windows_bridge_stopped: true` and
that no Windows helper remains running.
