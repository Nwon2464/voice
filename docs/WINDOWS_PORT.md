# WSL application with a native Windows bridge

The port is developed and run primarily in WSL. PySide6, Whisper, Codex App
Server, the session registry, prompt construction, and logs stay in WSL. A
minimal Windows Python helper handles only the two APIs that a Linux process
cannot use directly:

- WASAPI loopback capture of Windows Zoom, Teams, and browser output
- the Win32 global F8 hotkey

The WSL process starts this helper automatically through WSL interoperability.
Audio and F8 events travel over the helper's binary stdin/stdout channel. No
PowerShell window, Windows copy of the repository, or Windows Codex session is
required.

```text
Windows Zoom/Teams/browser output
              │
      WASAPI loopback + F8
        Windows helper
              │ binary stdio
              ▼
WSL PySide6 → Whisper small → current WSL Codex thread → Answer window
```

## Data boundary

Git transfers source, tests, documentation, and tracked benchmark records. The
following remain local and are not transferred automatically:

- WSL `.venv`
- the minimal Windows bridge environment under
  `%LOCALAPPDATA%\InterviewAssistantBridge\.venv`
- `test_runs/`, WAV, PCM, and runtime logs
- the WSL session list in `~/.config/interview-assistant/sessions.json`
- WSL Codex rollout and authentication data
- Whisper model caches

Because Codex stays in WSL, the application can use the existing WSL session
registry and rollouts. The Windows helper never reads Codex data.

## 1. WSL development environment

From the WSL repository root:

```bash
sudo apt update
sudo apt install python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-wsl.txt
```

If the distro provides GTK packages for the original Linux app, they are not
required by the PySide6 hybrid entry point. The original Linux GTK entry point
continues to use the setup in the main README.

## 2. Minimal Windows bridge environment

Windows Python is required only for the helper. From the same WSL shell, verify
that WSL interoperability can find it:

```bash
py.exe -3 --version
```

Then create the helper environment without opening PowerShell:

```bash
chmod +x setup_windows_bridge.sh
./setup_windows_bridge.sh
```

The script invokes Windows commands through WSL interoperability and installs
only NumPy and SoundCard. It does not install PySide6, Whisper, or Codex on
Windows.

## 3. WASAPI and F8 bridge probe

Play speech in a Windows Zoom, Teams, or browser window and press F8 during the
probe:

```bash
.venv/bin/python -m windows_port.audio_probe \
  --seconds 10 \
  --output wasapi_probe.wav
```

Success requires captured PCM, a `ready` status containing the Windows output
device, no bridge error, and `audio_detected: true` while speech is playing.
`f8_events` should be at least one when F8 was pressed. Listen to
`wasapi_probe.wav` and confirm normal-speed speech without microphone input.

To select a non-default Windows output endpoint, set its SoundCard device ID
before starting the probe or app:

```bash
export INTERVIEW_AUDIO_DEVICE_ID='device-id'
```

## 4. Whisper small benchmark in WSL

The WAV captured by the native helper is transcribed by the WSL model:

```bash
.venv/bin/python -m windows_port.whisper_benchmark \
  wasapi_probe.wav \
  --model small \
  --repeat 3
```

Record model load, warmup, and each transcription time. Do not compare a first
model download against a warm-cache Linux benchmark.

## 5. Full hybrid application

```bash
chmod +x start_wsl_windows_app.sh
./start_wsl_windows_app.sh
```

The session chooser reads the existing WSL app registry. Preparation chat and
F8 answers resume the same WSL Codex thread. The left arrow returns to chat and
`×` stops the Windows helper, audio stream, Whisper processes, and App Server.
All hybrid application UI labels and status messages are in English.

Optional environment variables:

```bash
export INTERVIEW_WHISPER_MODEL=small
export INTERVIEW_LANGUAGE=en
export INTERVIEW_CODEX_MODEL=gpt-5.6-sol
export INTERVIEW_CODEX_REASONING=low
export INTERVIEW_VAD_RMS=250
export INTERVIEW_WHISPER_CPU_THREADS=8
export INTERVIEW_PREVIEW_WHISPER_CPU_THREADS=8
export INTERVIEW_TEST_LOG=1
export INTERVIEW_TEST_LABEL=wsl-windows-manual
./start_wsl_windows_app.sh
```

Test logging writes private WAV and JSONL data under `test_runs/` and remains off
by default.

During speech, a separate Whisper `small` process repeatedly transcribes the
latest preview window. If inference is slower than the capture interval, the app
keeps the newest pending snapshot instead of building a stale queue. F8
terminates that process immediately, runs only the final-question transcription,
and starts a fresh preview process as soon as the question is committed.
Non-question utterances are not allowed to occupy the final Whisper worker.

## 6. Comparison with the Linux baseline

The hybrid controller retains the Linux event names for `whisper_ready`,
`preview_whisper_ready`, `f8_trigger`, `question`, `codex_app_server_ready`,
`codex_stream_start`, `codex_response`, and `app_session_end`. It additionally
records `windows_bridge_status` events.

Compare question-boundary accuracy, F8-to-question time, F8-to-first-visible
answer, full answer time, thread continuity, and cleanup of every helper and
worker.
