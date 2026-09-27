"""
=============================================================================
 SCRIBE DEVICES - which microphones exist, and noticing when that changes.
=============================================================================

 PortAudio (the audio library under sounddevice) lists every microphone
 several times - once per Windows audio "host API" - and some of those
 entries can't record at Scribe's 16 kHz at all. It also takes a snapshot of
 the devices when it starts, so a mic plugged in later, or a change of the
 Windows default mic, is invisible until it is re-initialized.

 This module hides all of that:
   - list_input_devices()  - each usable mic once, with its full name
   - resolve_device(name)  - a saved mic name -> the device to open
   - raw_twin(device)      - the same mic's WASAPI entry, for its RAW audio
   - refresh_portaudio()   - re-read the device list
   - default_capture_id()  - Windows' current default mic (Core Audio)
   - input_device_count()  - changes whenever a mic is plugged/unplugged
 The last two are cheap enough to poll every couple of seconds; app.py's
 device watcher uses them to reopen the mic only when something changed.
=============================================================================
"""

import ctypes
import os
import re

try:
    import sounddevice as sd
except Exception:            # no PortAudio at all: every query returns empty
    sd = None

# DirectSound gives full, correctly-encoded names and resamples to 16 kHz.
# MME also resamples but truncates names to 31 characters (and garbles ®).
# WASAPI / WDM-KS entries refuse 16 kHz, so they are never offered.
PREFERRED_HOST_APIS = ("Windows DirectSound", "MME")
# Each host API's stand-in for "whatever the Windows default is". The UI
# already offers "(system default)", so these are not listed separately.
PSEUDO_DEVICE_NAMES = ("Primary Sound Capture Driver",
                       "Microsoft Sound Mapper - Input")
SAMPLE_RATE = 16000


def _norm(name):
    """Letters and digits only, lowercased - so MME's truncated, mis-encoded
    'Microphone Array (Intel� Smart ' still matches the DirectSound name."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _host_api_index():
    """Index of the first preferred host API present, or None (use all)."""
    apis = sd.query_hostapis()
    for wanted in PREFERRED_HOST_APIS:
        for i, api in enumerate(apis):
            if api.get("name") == wanted:
                return i
    return None


def _candidates():
    """[(device index, name)] for every usable input on the chosen host API."""
    api = _host_api_index()
    out, seen = [], set()
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if api is not None and dev.get("hostapi") != api:
            continue
        name = (dev.get("name") or "").strip()
        if not name or name in PSEUDO_DEVICE_NAMES or name in seen:
            continue
        try:
            sd.check_input_settings(device=idx, channels=1,
                                    samplerate=SAMPLE_RATE, dtype="float32")
        except Exception:
            continue                      # can't record at 16 kHz - hide it
        seen.add(name)
        out.append((idx, name))
    return out


def list_input_devices():
    """Names of the microphones Scribe can actually record from, each once."""
    if sd is None:
        return []
    try:
        return [name for _idx, name in _candidates()]
    except Exception:
        return []


def _match(name):
    """(index, listed name) for a saved mic name, or (None, None)."""
    if not name or sd is None:
        return None, None
    try:
        cands = _candidates()
    except Exception:
        return None, None
    for idx, listed in cands:
        if listed == name:
            return idx, listed
    wanted = _norm(name)
    if wanted:
        for idx, listed in cands:
            got = _norm(listed)
            if got.startswith(wanted) or wanted.startswith(got):
                return idx, listed
    return None, None


def resolve_device(name):
    """The device index to open for a saved mic name, or None if that mic
    isn't connected. Matches exactly first, then by prefix, so names saved
    by older versions (MME's truncated form) still work."""
    return _match(name)[0]


def canonical_name(name):
    """The listed (full) name a saved mic name resolves to, or None."""
    return _match(name)[1]


def default_input_name():
    """A friendly name for the current default mic, or None."""
    if sd is None:
        return None
    try:
        raw = sd.query_devices(kind="input").get("name", "")
    except Exception:
        return None
    return canonical_name(raw) or raw or None


# =============================================================================
#  RAW AUDIO - the microphone as it really is, without the laptop's effects.
#
#  Many laptops run the mic through the maker's audio effects before any app
#  sees it - Dolby, Realtek, Windows Studio Effects. Their noise suppression
#  is tuned for calls: it gates whatever it takes for noise to silence and
#  dulls the consonants. Speech recognition does far better WITHOUT it
#  (measured on a Lenovo with Dolby: through the effects ElevenLabs heard
#  "Microbox, near to everything"; without them, the whole sentence, word
#  for word). Only WASAPI can ask Windows for a mic's raw audio, so the mic
#  Scribe chose is opened through its WASAPI entry when it has one.
# =============================================================================

WASAPI = "Windows WASAPI"
WASAPI_RAW = 1          # PortAudio's eStreamOptionRaw (pa_win_wasapi.h)


def raw_twin(device):
    """The WASAPI entry for the same microphone as `device` (an index from
    resolve_device(), or None for the Windows default), or None if it has
    none. Never raises."""
    if sd is None:
        return None
    try:
        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis) if a.get("name") == WASAPI), None)
        if wasapi is None:
            return None
        if device is None:
            idx = apis[wasapi].get("default_input_device", -1)
            return idx if isinstance(idx, int) and idx >= 0 else None
        devs = sd.query_devices()
        wanted = _norm(devs[device].get("name"))
        if not wanted:
            return None
        twins = [(idx, _norm(dev.get("name"))) for idx, dev in enumerate(devs)
                 if dev.get("hostapi") == wasapi and dev.get("max_input_channels", 0) > 0]
        # The same name first ("Headset (AirPods)" must not become
        # "Headset (AirPods Pro)"); a prefix only for MME's truncated names.
        for idx, got in twins:
            if got == wanted:
                return idx
        for idx, got in twins:
            if got and (got.startswith(wanted) or wanted.startswith(got)):
                return idx
    except Exception:
        return None
    return None


def raw_settings():
    """sounddevice settings that ask WASAPI for the mic's raw audio, converted
    by Windows to whatever rate and channels the stream asks for."""
    settings = sd.WasapiSettings(auto_convert=True)
    # sounddevice has no switch for this; the field is PortAudio's own
    # (devices_test checks it still exists).
    settings._streaminfo.streamOption = WASAPI_RAW
    return settings


def refresh_portaudio():
    """Re-initialize PortAudio so it sees plugged/unplugged/default-changed
    devices. Any open stream must be closed first (the caller does that)."""
    if sd is None:
        return
    # sounddevice keeps its last sound-cue playback stream open; PortAudio's
    # terminate frees it without telling sounddevice, and the next cue would
    # then touch freed memory. Close it properly first.
    try:
        sd.stop()
    except Exception:
        pass
    try:
        sd._terminate()
    finally:
        sd._initialize()        # never leave PortAudio down until a restart


# --- Windows Core Audio, via ctypes (no extra dependency) --------------------
# IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture, eConsole) ->
# IMMDevice::GetId gives a stable id string for the current default mic. We
# call the COM methods by their slot in the object's function table (vtable).

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_E_CAPTURE, _E_CONSOLE, _CLSCTX_ALL = 1, 0, 23


def _guid(text):
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _method(obj, slot, *argtypes):
    """The COM method in vtable `slot` of `obj`, as a callable."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])


def _release(obj):
    """IUnknown::Release (vtable slot 2)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def default_capture_id():
    """Windows' current default microphone as an id string, or None (no mic,
    not Windows, or COM unavailable). Safe to call from any thread."""
    if os.name != "nt":
        return None
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)   # per-thread; repeat calls are harmless
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        enum = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_MMDeviceEnumerator)), None,
                                  _CLSCTX_ALL, ctypes.byref(_guid(_IID_IMMDeviceEnumerator)),
                                  ctypes.byref(enum)) != 0 or not enum.value:
            return None
        try:
            dev = ctypes.c_void_p()
            get_default = _method(enum.value, 4, ctypes.c_int, ctypes.c_int,
                                  ctypes.POINTER(ctypes.c_void_p))
            if get_default(enum.value, _E_CAPTURE, _E_CONSOLE, ctypes.byref(dev)) != 0 \
                    or not dev.value:
                return None               # no capture device at all
            try:
                raw = ctypes.c_void_p()
                get_id = _method(dev.value, 5, ctypes.POINTER(ctypes.c_void_p))
                if get_id(dev.value, ctypes.byref(raw)) != 0 or not raw.value:
                    return None
                try:
                    return ctypes.wstring_at(raw.value)
                finally:
                    ole32.CoTaskMemFree(raw)
            finally:
                _release(dev.value)
        finally:
            _release(enum.value)
    except Exception:
        return None


def input_device_count():
    """Number of recording devices Windows knows about (legacy waveIn count).
    Changes whenever one is plugged in or removed. 0 off-Windows."""
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.winmm.waveInGetNumDevs())
    except Exception:
        return 0
