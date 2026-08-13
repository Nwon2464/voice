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

The Windows helper has no GUI, microphone capture, hotkey, Whisper, Moonshine,
or Codex dependencies.  It requests the default Windows output endpoint from
SoundCard and sends two binary frame types: `A` for PCM and `S` for UTF-8 JSON
status.  WSL sends `Q` on stdin to stop it.

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

## Scope boundary

No production application module imports `windows_port` in this stage.  Do not
wire this probe into `MoonshineStreamingWorker`, F8/F9, the launcher, sessions,
or `interview_app.py` until the capture result has been manually verified on a
Windows host.
