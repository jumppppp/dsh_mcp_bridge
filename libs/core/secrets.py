"""User-bound Windows DPAPI storage for Web connection credentials."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import uuid


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Secure credential persistence requires Windows DPAPI")
    buffer = ctypes.create_string_buffer(value)
    source = _Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; never prompt or fall back to plaintext.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows credential protection failed (error {})".format(ctypes.get_last_error()))
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def write_secret_json(path: Path, value: dict) -> None:
    payload = _crypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    envelope = {"format": "dpapi-user-v1", "payload": base64.b64encode(payload).decode("ascii")}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(envelope), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_secret_json(path: Path) -> dict:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    if envelope.get("format") == "dpapi-user-v1":
        return json.loads(_crypt(base64.b64decode(envelope["payload"], validate=True), decrypt=True))
    # Existing credentials remain readable; the next authorized save encrypts
    # them. Reading never rewrites a live service's state or credentials.
    if "format" in envelope:
        raise ValueError("Unknown credential storage format")
    return envelope
