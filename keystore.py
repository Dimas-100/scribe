"""
=============================================================================
 SCRIBE KEYSTORE - where your API key is kept.
=============================================================================

 An API key is a password: anyone who has it can spend your quota. So it
 does NOT live in config.json - a plain text file people back up, zip up and
 paste into bug reports. It lives in Windows Credential Manager, the vault
 Windows uses for your network and Git passwords: encrypted with your
 Windows sign-in and readable only by you, on this PC.

   get_key()              - the key in the vault, or ""
   resolve_key(cfg)       - the key to use: the vault's, else config.json's
   set_key(value)         - save it; returns "vault" or "file" (see below)
   delete_key()           - forget it (vault and file)
   key_hint(key)          - "ends in 1a2b": show a key without showing it
   migrate_from_config()  - move an older version's key out of config.json

 Each cloud service has its own key (see SERVICES): every function above
 takes service="groq" (the default) or service="elevenlabs".

 Two fallbacks keep Scribe working when the vault can't be used:
   - SCRIBE_DATA_DIR is set (the tests, or a portable copy): the key stays
     in that folder's config.json and the real vault is never touched.
   - Credential Manager refuses (a locked-down PC): the key stays in
     config.json, and the user is told once.

 ctypes only (advapi32's Cred* functions) - no extra dependency.
=============================================================================
"""

import ctypes
from ctypes import wintypes

import storage

CONFIG_KEY = "groq_api_key"   # where older versions (and the fallback) keep it
TARGET = "Scribe/groq"        # the credential's name in Credential Manager

# Each cloud service's key: its name in Credential Manager, and the
# config.json field used only as the fallback (tests, portable copies, a
# vault that refuses). Groq's pair is the original TARGET / CONFIG_KEY.
SERVICES = {
    "groq":       (TARGET, CONFIG_KEY),
    "elevenlabs": ("Scribe/elevenlabs", "elevenlabs_api_key"),
}

_CRED_TYPE_GENERIC = 1            # an app's own secret (not a Windows logon)
_CRED_PERSIST_LOCAL_MACHINE = 2   # survives sign-out; this PC only, not roamed
_ERROR_NOT_FOUND = 1168

KEY_STORE_FAILED = {
    "key": "key_store_failed", "title": "Couldn't secure your API key",
    "message": "Windows Credential Manager isn't available, so your key "
               "stays in Scribe's settings file.",
}

# Credential Manager refused once this run: don't retry (and re-notify) on
# every settings reload. Reset by a restart.
_vault_failed = False


class VaultError(Exception):
    """Credential Manager refused a read/write/delete."""


class _CREDENTIAL(ctypes.Structure):
    """Win32 CREDENTIALW - the record Credential Manager stores."""
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi():
    """advapi32 with its Cred* signatures declared, or None off-Windows."""
    try:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    lib.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    lib.CredReadW.restype = wintypes.BOOL
    lib.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    lib.CredDeleteW.restype = wintypes.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


def _vault_read(target):
    """The secret stored under `target`, or None if there is none."""
    lib = _advapi()
    if lib is None:
        raise VaultError("Credential Manager isn't available")
    pcred = ctypes.POINTER(_CREDENTIAL)()
    if not lib.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise VaultError(f"CredRead failed (error {err})")
    try:
        cred = pcred.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        lib.CredFree(pcred)       # Windows allocated it; Windows frees it


def _vault_write(target, secret):
    """Store `secret` (non-empty text) under `target`, replacing any old one."""
    lib = _advapi()
    if lib is None:
        raise VaultError("Credential Manager isn't available")
    data = secret.encode("utf-16-le")     # the convention for generic secrets
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "Scribe"
    if not lib.CredWriteW(ctypes.byref(cred), 0):
        raise VaultError(f"CredWrite failed (error {ctypes.get_last_error()})")


def _vault_delete(target):
    """Remove `target`. Already gone counts as success."""
    lib = _advapi()
    if lib is None:
        raise VaultError("Credential Manager isn't available")
    if not lib.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        err = ctypes.get_last_error()
        if err != _ERROR_NOT_FOUND:
            raise VaultError(f"CredDelete failed (error {err})")


def _file_mode():
    """True when the key must stay in config.json: an override data folder
    (tests, portable use) must never read or write the real vault."""
    return storage.USING_OVERRIDE


def uses_vault():
    """True when keys go to Credential Manager (not an override folder)."""
    return not _file_mode()


def get_key(service="groq"):
    """The service's key saved in Credential Manager, or "" (none, file mode,
    or the vault couldn't be read - logged)."""
    target, _field = SERVICES[service]
    if _file_mode():
        return ""
    try:
        return _vault_read(target) or ""
    except VaultError as exc:
        storage.log_error("read API key", exc)
        return ""


def resolve_key(cfg, service="groq"):
    """The key Scribe should use for `service`: the vault's, else
    config.json's (the fallbacks, or an older version's not yet migrated)."""
    _target, field = SERVICES[service]
    return get_key(service) or (cfg.get(field) or "").strip()


def _clear_config_copy(field):
    """Remove a key from config.json once the vault holds it. Never creates
    config.json (on a first run it must not exist until setup is done)."""
    cfg, _notices = storage.load_config()
    if cfg.get(field):
        try:
            storage.save_config_changes({field: ""})
        except storage.StorageError as exc:
            # The vault copy wins anyway (resolve_key); retried next time.
            storage.log_error("clear API key from config.json", exc)


def set_key(value, service="groq"):
    """
    Save `value` as the service's API key. Returns "vault" (Credential
    Manager) or "file" (config.json - an override folder, or the vault
    refused, logged). Raises ValueError for a blank key,
    storage.StorageError if it can't be saved anywhere.
    """
    target, field = SERVICES[service]
    value = (value or "").strip()
    if not value:
        raise ValueError("an API key can't be blank")
    if not _file_mode():
        try:
            _vault_write(target, value)
            if _vault_read(target) == value:
                _clear_config_copy(field)
                return "vault"
            storage.log_error("save API key",
                              message="Credential Manager read back a different value")
        except VaultError as exc:
            storage.log_error("save API key", exc)
    storage.save_config_changes({field: value})
    return "file"


def delete_key(service="groq"):
    """Forget the service's key everywhere it could be."""
    target, field = SERVICES[service]
    if not _file_mode():
        try:
            _vault_delete(target)
        except VaultError as exc:
            storage.log_error("delete API key", exc)
    _clear_config_copy(field)


def key_hint(key):
    """A safe way to show a saved key: its last four characters."""
    key = (key or "").strip()
    if not key:
        return ""
    return f"ends in {key[-4:]}" if len(key) >= 8 else "saved"


def migrate_from_config(cfg):
    """
    Older versions kept the key in config.json (and a refusing vault leaves
    it there). Move every service's key into the vault - write, read back,
    compare - and only then clear it from config.json (whose .bak refreshes
    with it). Returns notices for the user (the vault refused - at most
    one). Does nothing in file mode or when config.json has no key.
    """
    global _vault_failed
    if _file_mode():
        return []
    for target, field in SERVICES.values():
        old = (cfg.get(field) or "").strip()
        if not old or _vault_failed:
            continue
        try:
            _vault_write(target, old)
            moved = _vault_read(target) == old
        except VaultError as exc:
            storage.log_error("move API key to Credential Manager", exc)
            moved = False
        if not moved:
            _vault_failed = True
            storage.log_error("move API key", message="kept in config.json")
            return [dict(KEY_STORE_FAILED)]
        try:
            storage.save_config_changes({field: ""})
        except storage.StorageError as exc:
            storage.log_error("clear API key from config.json", exc)
    return []
