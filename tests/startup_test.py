r"""
Standalone probe for app.py's startup: settings loading, recovery and the
data folder. Every import uses tests/harness.py (temp data folder, mocks).

Run from the project root:   venv\Scripts\python tests\startup_test.py
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, import_app_with_mocks  # noqa: E402

# app.py SETTINGS constant for each storage.DEFAULT_CONFIG key.
CONSTANT_FOR_KEY = {
    "user_name": "USER_NAME", "hotkey": "current_hotkey_name",
    "model_size": "MODEL_SIZE", "mic_device": "MIC_DEVICE",
    "add_trailing_space": "ADD_TRAILING_SPACE", "paste_mode": "PASTE_MODE",
    "sound_cues": "SOUND_CUES", "use_cloud": "USE_CLOUD",
    "groq_api_key": "GROQ_API_KEY", "cloud_model": "CLOUD_MODEL",
    "pipeline_cloud": "PIPELINE_CLOUD", "local_model": "LOCAL_MODEL",
    "cloud_provider": "CLOUD_PROVIDER", "elevenlabs_api_key": "ELEVENLABS_API_KEY",
    "polish": "POLISH", "polish_style": "POLISH_STYLE", "theme": "THEME",
    "check_updates": "CHECK_UPDATES", "update_notified": "UPDATE_NOTIFIED",
}


def test_settings_constants_match_storage_defaults():
    import storage
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    literals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                literals.setdefault(node.targets[0].id, ast.literal_eval(node.value))
            except ValueError:
                pass
    for key, const in CONSTANT_FOR_KEY.items():
        assert literals.get(const) == storage.DEFAULT_CONFIG[key], \
            f"app.py {const}={literals.get(const)!r} but DEFAULT_CONFIG[{key!r}]=" \
            f"{storage.DEFAULT_CONFIG[key]!r}"
    print("PASS  app.py's SETTINGS constants match storage.DEFAULT_CONFIG.")


def test_uses_data_folder():
    app = import_app_with_mocks()
    assert app.CONFIG_FILE == os.path.join(app._test_data_dir, "config.json")
    assert app.LOG_FILE.startswith(app._test_data_dir)
    assert app.MIGRATED == [], "migration must not run under SCRIBE_DATA_DIR"
    assert sorted(app.HOTKEY_CHOICES) == sorted(__import__("storage").HOTKEY_NAMES)
    print("PASS  app.py reads/writes the data folder and hotkey names agree.")


def test_a_missing_library_says_so():
    # pythonw shows nothing on its own: a library that won't load (no Visual
    # C++ runtime on a fresh PC) must end in a message box, not silence.
    # Run in a child process on a throwaway data folder; the message box is
    # replaced by a print.
    import subprocess
    import tempfile
    code = "\n".join([
        "import sys, ctypes, runpy",
        "ctypes.windll.user32.MessageBoxW = lambda h, text, title, f: print('BOX', title, '|', text)",
        "sys.modules['pynput'] = None",                    # 'import pynput' now fails
        "sys.argv = ['app.py']",
        f"runpy.run_path({os.path.join(ROOT, 'app.py')!r}, run_name='__main__')",
    ])
    env = dict(os.environ, SCRIBE_DATA_DIR=tempfile.mkdtemp(prefix="scribe-missing-"))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert "BOX Scribe couldn't start | Part of Scribe is missing" in r.stdout, r.stdout[-400:]
    assert "setup.bat" in r.stdout and "vc_redist" in r.stdout
    print("PASS  a library that won't load ends in a clear message box, not silence.")


def test_hotkey_list_does_not_crash():
    app = import_app_with_mocks(config={"hotkey": ["Ctrl", "Win"], "paste_mode": False})
    assert app.current_hotkey_name == "Ctrl + Win" and app.PASTE_MODE is False
    assert "settings_invalid" in [n["key"] for n in app.STARTUP_NOTICES]
    print("PASS  a list-valued hotkey no longer crashes startup; only it resets.")


def test_string_bool_is_false():
    app = import_app_with_mocks(config={"use_cloud": "false", "groq_api_key": "k"})
    assert app.USE_CLOUD is False
    print("PASS  \"use_cloud\": \"false\" means off.")


def test_corrupt_config_is_reported_not_fatal():
    app = import_app_with_mocks(config_text="{oops")
    assert app.current_hotkey_name == "Ctrl + Win"
    assert [n["key"] for n in app.STARTUP_NOTICES] == ["settings_reset"]
    kept = [n for n in os.listdir(app._test_data_dir) if n.startswith("config.corrupt-")]
    assert kept, "damaged config must be kept aside"
    print("PASS  a damaged config at startup is kept aside and reported.")


def test_dictation_log_roundtrip():
    app = import_app_with_mocks()
    app.log_dictation("hello world", 1.5)
    app.log_dictation("second", 0.8)
    app.remove_last_log_entry()
    import storage
    assert [e["text"] for e in storage.read_jsonl(app.LOG_FILE)] == ["hello world"]
    print("PASS  log_dictation / remove_last_log_entry go through storage.")


def test_save_config_failure_is_readable():
    app = import_app_with_mocks()
    import storage
    real = storage.atomic_write_json

    def denied(*args, **kwargs):
        raise PermissionError("denied")
    storage.atomic_write_json = denied
    try:
        try:
            app.save_config()
        except storage.StorageError as exc:
            assert "settings" in str(exc).lower()
        else:
            raise AssertionError("save_config must raise StorageError on a write failure")
    finally:
        storage.atomic_write_json = real
    print("PASS  save_config turns a write failure into a readable StorageError.")


def test_single_instance_guard_runs_before_migration():
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert src.index("instance.acquire()") < src.index("storage.migrate_legacy_files()"), \
        "a second launch must exit BEFORE it moves the running copy's files"
    assert "47200" not in src and "import socket" not in src, "the TCP port is gone"
    print("PASS  the single-instance guard runs before any file is migrated.")


def test_overlay_scales_with_dpi():
    app = import_app_with_mocks()
    assert app._px(56, scale=1.0) == 56
    assert app._px(56, scale=1.5) == 84 and app._px(4, scale=1.25) == 5
    assert app._px(1, scale=1.0) >= 1
    assert app.OVERLAY_W == app._px(56) and app.OVERLAY_H_IDLE == app._px(4)
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    assert src.index('sys.platform != "win32"') < src.index("import numpy"), \
        "the Windows-only check must run before the Windows-only imports"
    print("PASS  overlay sizes scale with display DPI; non-Windows exits early.")


def test_second_launch_exits_before_heavy_imports():
    """A click on Scribe while it's running must answer at once: the
    single-instance check runs before numpy/Whisper/Tk are imported (they
    take ~2 s), not after."""
    src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    guard = src.index("instance.acquire()")
    for heavy in ("import numpy", "import pystray", "from faster_whisper", "import tkinter"):
        assert guard < src.index(heavy), f"the guard must come before {heavy!r}"
    print("PASS  a second launch exits before the slow imports.")


def test_control_messages_route_to_the_main_thread():
    app = import_app_with_mocks()
    drained = []
    while not app.ui_queue.empty():
        drained.append(app.ui_queue.get_nowait())
    assert app._handle_control({"cmd": "show"}) is None
    assert app._handle_control({"cmd": "reload-config"}) is None
    assert app._handle_control({"cmd": "nonsense"}) is None
    got = []
    while not app.ui_queue.empty():
        item = app.ui_queue.get_nowait()
        if not isinstance(item, tuple):          # skip status refreshes
            got.append(item)
    assert got == ["open", "reload-config"], got
    status = app._handle_control({"cmd": "status"})
    assert status["hotkey"] == "Ctrl + Win" and status["cloud"] is False
    assert status["activity"] == "idle" and status["provider"] == "local", status
    app.recording = True
    assert app._handle_control({"cmd": "status"})["activity"] == "connecting"   # no audio yet
    app._audio_flowing.set()
    assert app._handle_control({"cmd": "status"})["activity"] == "recording"
    app._audio_flowing.clear()
    app.recording = False
    app._in_flight = 1
    assert app._handle_control({"cmd": "status"})["activity"] == "transcribing"
    app._in_flight = 0
    app.USE_CLOUD, app.CLOUD_PROVIDER = True, "elevenlabs"
    assert app._handle_control({"cmd": "status"})["provider"] == "elevenlabs"
    app.USE_CLOUD, app.CLOUD_PROVIDER = False, "groq"
    print("PASS  pipe commands are handed to the main thread; status replies.")


def test_migration_notices_and_first_run():
    app = import_app_with_mocks()
    notes = app._migration_notices({"conflicts": ["dictation_log.jsonl"], "failed": ["config.json"]})
    assert [n["key"] for n in notes] == ["migrate_conflict", "migrate_failed"], notes
    assert app._migration_notices({"conflicts": [], "failed": []}) == []
    assert app._is_first_run(False, {"conflicts": [], "failed": ["config.json"]}) is False
    assert app._is_first_run(False, {"conflicts": [], "failed": []}) is True
    assert app._is_first_run(True, {"conflicts": [], "failed": []}) is False
    print("PASS  migration problems become notices; a failed config move isn't a first run.")


def _luma(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def test_the_pill_is_neutral():
    """The recording pill floats over every app, so it follows no theme: a
    near-black capsule with a hairline edge, white bars while recording,
    soft grey while transcribing - and no coloured glow."""
    from unittest import mock
    from PIL import ImageFilter
    app = import_app_with_mocks()
    w, h = app.OVERLAY_W, app.OVERLAY_H
    levels = [0.9, 0.2, 0.6, 1.0, 0.4, 0.8, 0.3, 0.7, 0.5, 0.9, 0.1, 0.6]
    # No blur anywhere: the glow is gone, not just dimmed.
    with mock.patch.object(ImageFilter, "GaussianBlur", side_effect=AssertionError("a glow")):
        rec = app._render_pill(w, h, app.OVERLAY_FILL, levels, app.WAVE_RECORDING)
        busy = app._render_pill(w, h, app.OVERLAY_FILL, levels, app.WAVE_TRANSCRIBING)
        plain = app._render_pill(w, h, app.OVERLAY_FILL)
    assert rec.size == (w, h)
    # Neutral: no mint, no amber - every visible pixel is a grey.
    for img in (rec, busy, plain):
        for px in img.getdata():
            if px != (0, 0, 0):
                assert max(px) - min(px) <= 10, px
    # Recording's bars are bright; transcribing's are a softer grey.
    assert max(map(_luma, rec.getdata())) > max(map(_luma, busy.getdata())) + 40
    # A hairline edge: the pill's top row is lighter than its middle.
    cx = w // 2
    assert _luma(plain.getpixel((cx, 0))) > _luma(plain.getpixel((cx, h // 2))) + 8
    # The idle pill's quota nudge is still visible: a warm / red tint you can
    # tell apart from the neutral fill.
    iw, ih = app.OVERLAY_W_IDLE, app.OVERLAY_H_IDLE
    mid = (cx, h - ih // 2)
    calm = app._render_pill(iw, ih, app.OVERLAY_FILL).getpixel(mid)
    for tint in (app.OVERLAY_FILL_WARN, app.OVERLAY_FILL_DANGER):
        tinted = app._render_pill(iw, ih, tint).getpixel(mid)
        assert (_luma(tinted) + 12) / (_luma(calm) + 12) >= 1.35, (tint, tinted, calm)
    print("PASS  the pill is neutral: grey capsule, hairline edge, white/grey bars, no glow.")


def test_second_launch_retries():
    app = import_app_with_mocks()
    replies = iter([False, False, True])
    naps = []
    assert app._hand_off_to_running_copy(lambda msg: next(replies), naps.append) is True
    assert len(naps) == 2
    assert app._hand_off_to_running_copy(lambda msg: False, naps.append, attempts=3) is False
    print("PASS  a second launch keeps trying to reach the first.")


if __name__ == "__main__":
    test_settings_constants_match_storage_defaults()
    test_uses_data_folder()
    test_a_missing_library_says_so()
    test_hotkey_list_does_not_crash()
    test_string_bool_is_false()
    test_corrupt_config_is_reported_not_fatal()
    test_dictation_log_roundtrip()
    test_save_config_failure_is_readable()
    test_single_instance_guard_runs_before_migration()
    test_control_messages_route_to_the_main_thread()
    test_overlay_scales_with_dpi()
    test_second_launch_exits_before_heavy_imports()
    test_the_pill_is_neutral()
    test_second_launch_retries()
    test_migration_notices_and_first_run()
    print("\nAll startup tests passed.")
