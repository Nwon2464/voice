"""Small platform-transport selection boundary for the shared interview flow."""

PULSEAUDIO_AUDIO_BACKEND = "pulseaudio"
WINDOWS_BRIDGE_AUDIO_BACKEND = "windows_bridge"
SUPPORTED_AUDIO_BACKENDS = frozenset({
    PULSEAUDIO_AUDIO_BACKEND,
    WINDOWS_BRIDGE_AUDIO_BACKEND,
})


def create_platform_backend(
    backend,
    *,
    worker,
    on_pcm,
    on_error,
    on_f8,
    on_f9,
    on_stop,
    on_status,
    gio=None,
    idle_add=None,
    app_command_path=None,
    is_running=None,
):
    """Create only the selected OS transport; semantic callbacks stay shared."""
    if backend == PULSEAUDIO_AUDIO_BACKEND:
        from linux_port.backend import LinuxPlatformBackend

        return LinuxPlatformBackend(
            on_pcm,
            on_error,
            on_f8,
            on_f9,
            on_stop,
            on_status,
            gio=gio,
            idle_add=idle_add,
            app_command_path=app_command_path,
            is_running=is_running,
        )
    if backend == WINDOWS_BRIDGE_AUDIO_BACKEND:
        from windows_port.app_backend import WindowsPlatformBackend

        return WindowsPlatformBackend(
            worker,
            on_pcm,
            on_error,
            on_f8,
            on_f9,
            on_status,
        )
    raise ValueError(f"unsupported audio backend: {backend}")
