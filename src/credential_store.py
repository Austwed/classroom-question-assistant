"""Store API keys in the current Windows user's Credential Manager vault."""

from __future__ import annotations

import ctypes
import hashlib
import sys
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_BLOB_BYTES = 2560
PREFIX = "CodexProject.ClassroomQuestionAssistant.ApiKey"


class _Credential(ctypes.Structure):
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


def target_for(provider: str, base_url: str = "") -> str:
    if provider == "openai":
        return PREFIX + ".OpenAI"
    if provider == "compatible":
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("请先填写兼容 API 地址")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return PREFIX + ".Compatible." + digest
    raise ValueError("本地模型不需要 API Key")


def _api():
    if sys.platform != "win32":
        raise OSError("API Key 自动保存仅支持 Windows")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredWriteW.argtypes = (ctypes.POINTER(_Credential), wintypes.DWORD)
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    )
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = (ctypes.c_void_p,)
    advapi.CredFree.restype = None
    return advapi


def _failure(operation: str) -> OSError:
    return OSError(ctypes.get_last_error(), f"Windows 凭据库{operation}失败")


def save_key(target: str, key: str) -> None:
    secret = key.strip().encode("utf-8")
    if not secret:
        raise ValueError("API Key 不能为空")
    if len(secret) > MAX_BLOB_BYTES:
        raise ValueError("API Key 过长，无法保存到 Windows 凭据库")
    blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
    credential = _Credential()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "通用语音问答助手 API Key"
    credential.CredentialBlobSize = len(secret)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = "api-key"
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise _failure("保存")


def load_key(target: str) -> str | None:
    api = _api()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise _failure("读取")
    try:
        credential = pointer.contents
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def delete_key(target: str) -> bool:
    if _api().CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    if ctypes.get_last_error() == ERROR_NOT_FOUND:
        return False
    raise _failure("删除")
