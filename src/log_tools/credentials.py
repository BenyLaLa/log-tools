"""Read only explicitly referenced secrets; never enumerate or log credential values."""
from __future__ import annotations

import os


def windows_environment(name):
    if os.name != "nt":
        return None
    import winreg
    for hive, path in ((winreg.HKEY_CURRENT_USER, "Environment"), (winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                if isinstance(value, str) and value:
                    return value
        except OSError:
            pass
    return None


def _api():
    import ctypes as ct
    from ctypes import wintypes as wt

    class Credential(ct.Structure):
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
                    ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
                    ("CredentialBlobSize", wt.DWORD), ("CredentialBlob", ct.POINTER(wt.BYTE)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ct.c_void_p), ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]
    api = ct.WinDLL("Advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ct.POINTER(Credential), wt.DWORD]
    api.CredWriteW.restype = wt.BOOL
    api.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ct.POINTER(ct.POINTER(Credential))]
    api.CredReadW.restype = wt.BOOL
    api.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
    api.CredDeleteW.restype = wt.BOOL
    api.CredFree.argtypes = [ct.c_void_p]
    return ct, wt, Credential, api


def _target(target):
    if not target.startswith("LogTools/") or len(target) > 240 or any(ord(c) < 32 for c in target):
        raise ValueError("Invalid LogTools credential reference")


def read_credential(target):
    _target(target)
    if os.name != "nt":
        return None
    ct, _, credential, api = _api()
    result = ct.POINTER(credential)()
    if not api.CredReadW(target, 1, 0, ct.byref(result)):
        if ct.get_last_error() == 1168:
            return None
        raise OSError("Windows credential store is unavailable for this user")
    try:
        return ct.string_at(result.contents.CredentialBlob,
                            result.contents.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(result)


def write_credential(target, value):
    _target(target)
    if os.name != "nt":
        raise ValueError("Windows credential storage requires Windows")
    blob = value.encode("utf-16-le")
    if not blob or len(blob) > 2560:
        raise ValueError("Credential must contain 1 to 2560 encoded bytes")
    ct, wt, credential, api = _api()
    buffer = (wt.BYTE * len(blob)).from_buffer_copy(blob)
    item = credential(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                      CredentialBlob=buffer, Persist=2, UserName="LogTools")
    if not api.CredWriteW(ct.byref(item), 0):
        raise OSError("Could not save credential for the current Windows user")


def delete_credential(target):
    _target(target)
    ct, _, _, api = _api()
    if not api.CredDeleteW(target, 1, 0) and ct.get_last_error() != 1168:
        raise OSError("Could not remove Windows credential")
