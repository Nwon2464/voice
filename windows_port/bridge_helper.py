"""Native Windows WASAPI and optional global-hotkey helper over binary stdout."""

from __future__ import annotations

import argparse
import os
import sys
import threading

from windows_port.audio import WasapiLoopbackCapture, list_output_devices
from windows_port.bridge_protocol import AUDIO, encode_frame, encode_hotkey, encode_status
from windows_port.hotkey import GlobalHotkeys, HotkeyRegistrationError


class FrameWriter:
    """Serialize concurrent capture and status writes to binary stdout."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout.buffer
        self.lock = threading.Lock()

    def write(self, frame: bytes) -> None:
        with self.lock:
            self.stream.write(frame)
            self.stream.flush()

    def status(self, event: str, **fields) -> None:
        self.write(encode_status({"event": event, **fields}))

    def hotkey(self, event: dict) -> None:
        self.write(encode_hotkey(
            event["key"], event["sequence"], event["timestamp_ns"]
        ))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Capture default Windows output through WASAPI loopback."
    )
    parser.add_argument("--device-id", help="optional SoundCard output device id")
    parser.add_argument(
        "--hotkeys", action="store_true", help="register global Windows F8 and F9"
    )
    parser.add_argument(
        "--no-audio", action="store_true", help="do not start WASAPI capture"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    writer = FrameWriter()
    capture = None
    hotkeys = None
    if os.name != "nt":
        writer.status("error", error="bridge_helper must run with Windows Python")
        return 1

    try:
        if not args.no_audio:
            capture = WasapiLoopbackCapture(
                lambda pcm: writer.write(encode_frame(AUDIO, pcm)),
                lambda error: writer.status("audio_error", error=str(error)),
                speaker_id=args.device_id,
            )
            capture.start()
        if args.hotkeys:
            hotkeys = GlobalHotkeys(writer.hotkey)
            hotkeys.start()
        writer.status(
            "ready",
            device=None if capture is None else capture.device_info,
            devices=[] if capture is None else list_output_devices(),
            audio_enabled=not args.no_audio,
            hotkeys_enabled=args.hotkeys,
        )
        # The WSL client closes stdin or sends Q when the probe has finished.
        while True:
            command = sys.stdin.buffer.read(1)
            if not command or command == b"Q":
                break
    except HotkeyRegistrationError as error:
        writer.status("hotkey_error", error=str(error))
        return 1
    except Exception as error:
        writer.status("error", error=str(error))
        return 1
    finally:
        if hotkeys is not None:
            hotkeys.stop()
        if capture is not None:
            capture.stop()
        try:
            writer.status("stopped")
        except (BrokenPipeError, OSError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
