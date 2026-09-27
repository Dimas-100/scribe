r"""
Standalone probe for devices.py - which microphones Scribe offers and uses.

Run from the project root:   venv\Scripts\python tests\devices_test.py

The list/resolve checks use a fake device table (no real audio calls). The
last check makes one real, read-only Windows call to confirm the Core Audio
lookup works on this machine.
"""

import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import devices  # noqa: E402

HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2},
    {"name": "Microphone Array (Intel� Smart ", "hostapi": 0, "max_input_channels": 4},
    {"name": "Primary Sound Capture Driver", "hostapi": 1, "max_input_channels": 2},
    {"name": "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)",
     "hostapi": 1, "max_input_channels": 4},
    {"name": "Headset (AirPods)", "hostapi": 1, "max_input_channels": 1},
    {"name": "Stereo Mix (Realtek)", "hostapi": 1, "max_input_channels": 2},
    {"name": "PC Speaker", "hostapi": 1, "max_input_channels": 0},
    {"name": "Headset (AirPods)", "hostapi": 2, "max_input_channels": 1},
]


def fake_check(device=None, **kwargs):
    if "Stereo Mix" in DEVICES[device]["name"]:
        raise Exception("Invalid sample rate")


def patched():
    return mock.patch.multiple(
        devices.sd, query_hostapis=mock.MagicMock(return_value=HOSTAPIS),
        query_devices=mock.MagicMock(return_value=DEVICES),
        check_input_settings=mock.MagicMock(side_effect=fake_check))


def test_list_only_usable_devices_once():
    with patched():
        names = devices.list_input_devices()
    assert names == [
        "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)",
        "Headset (AirPods)"], names
    print("PASS  lists one host API, skips pseudo/failing/output devices, no duplicates.")


def test_resolve_exact_prefix_and_missing():
    with patched():
        assert devices.resolve_device("Headset (AirPods)") == 4
        # A legacy config holds MME's truncated, mis-encoded name.
        assert devices.resolve_device("Microphone Array (Intel� Smart ") == 3
        assert devices.canonical_name("Microphone Array (Intel� Smart ") == (
            "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)")
        assert devices.resolve_device("USB Mic (unplugged)") is None
        assert devices.resolve_device(None) is None
    print("PASS  resolve_device: exact, legacy-truncated prefix, and missing.")


def test_audio_backend_failure_is_empty_not_crash():
    with mock.patch.object(devices.sd, "query_devices", side_effect=RuntimeError("no PortAudio")):
        assert devices.list_input_devices() == []
        assert devices.resolve_device("Headset (AirPods)") is None
    print("PASS  an unavailable audio backend yields an empty list, not a crash.")


def test_real_windows_signals():
    cid = devices.default_capture_id()
    count = devices.input_device_count()
    assert cid is None or (isinstance(cid, str) and cid.startswith("{")), cid
    assert isinstance(count, int) and count >= 0
    print(f"PASS  Core Audio default-capture id and device count readable "
          f"(count={count}, id={'set' if cid else 'none'}).")


def test_refresh_closes_cue_stream_first_and_always_reinitializes():
    calls = []
    with mock.patch.object(devices.sd, "stop", side_effect=lambda: calls.append("stop")), \
         mock.patch.object(devices.sd, "_terminate", side_effect=RuntimeError("boom")), \
         mock.patch.object(devices.sd, "_initialize", side_effect=lambda: calls.append("init")):
        try:
            devices.refresh_portaudio()
        except RuntimeError:
            pass
    assert calls == ["stop", "init"], calls
    print("PASS  refresh closes the sound-cue stream first and always re-initializes.")

def test_raw_twin_is_the_same_mic_on_wasapi():
    """Raw audio (no Dolby / noise suppression) is only offered by WASAPI, so
    the mic Scribe resolved (a DirectSound entry, or the Windows default) is
    opened through its WASAPI entry - found by name."""
    hostapis = [dict(HOSTAPIS[0]), dict(HOSTAPIS[1]), dict(HOSTAPIS[2], default_input_device=8)]
    devs = DEVICES + [{"name": "Microphone Array (Intel® Smart Sound Technology for Digital Microphones)",
                       "hostapi": 2, "max_input_channels": 4}]
    with mock.patch.multiple(devices.sd, query_hostapis=mock.MagicMock(return_value=hostapis),
                             query_devices=mock.MagicMock(return_value=devs)):
        assert devices.raw_twin(4) == 7          # DirectSound AirPods -> WASAPI AirPods
        assert devices.raw_twin(3) == 8          # the laptop array -> its WASAPI entry
        assert devices.raw_twin(None) == 8       # "(system default)" -> WASAPI's default
        assert devices.raw_twin(5) is None       # no WASAPI entry for this one
    with mock.patch.multiple(devices.sd, query_hostapis=mock.MagicMock(return_value=HOSTAPIS[:2]),
                             query_devices=mock.MagicMock(return_value=DEVICES)):
        assert devices.raw_twin(None) is None    # no WASAPI at all
        assert devices.raw_twin(3) is None
    with mock.patch.object(devices.sd, "query_hostapis", side_effect=Exception("PortAudio down")):
        assert devices.raw_twin(3) is None       # never raises
    # The real sounddevice struct: raw mode on (fails loudly if an upgrade
    # renames the private field this relies on).
    settings = devices.raw_settings()
    assert settings._streaminfo.streamOption == devices.WASAPI_RAW == 1
    print("PASS  the chosen mic's WASAPI twin is found by name; raw mode is set.")


if __name__ == "__main__":
    test_raw_twin_is_the_same_mic_on_wasapi()
    test_list_only_usable_devices_once()
    test_resolve_exact_prefix_and_missing()
    test_audio_backend_failure_is_empty_not_crash()
    test_real_windows_signals()
    test_refresh_closes_cue_stream_first_and_always_reinitializes()
    print("\nAll device tests passed.")
