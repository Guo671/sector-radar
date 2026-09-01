from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes


class SecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(protect: bool, data: bytes) -> bytes:
    if not sys.platform.startswith("win"):
        raise SecretError("API Key只能在Windows中使用DPAPI加密保存；当前系统仅支持环境变量测试。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob(data)
    result = _DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "SectorRadar DeepSeek API Key" if protect else None
    ok = function(ctypes.byref(source), description, None, None, None, flags, ctypes.byref(result))
    _ = source_buffer
    if not ok:
        raise SecretError(f"Windows DPAPI操作失败，错误码{kernel32.GetLastError()}。")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def encrypt_for_current_user(secret: str) -> str:
    if not secret.strip():
        return ""
    return base64.b64encode(_dpapi(True, secret.strip().encode("utf-8"))).decode("ascii")


def decrypt_for_current_user(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        raw = base64.b64decode(ciphertext.encode("ascii"), validate=True)
    except ValueError as exc:
        raise SecretError("已保存的API Key密文损坏。") from exc
    return _dpapi(False, raw).decode("utf-8")


def api_key_from_db(db) -> str:
    environment_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if environment_key:
        return environment_key
    encrypted = db.setting("deepseek_api_key_dpapi", "")
    return decrypt_for_current_user(encrypted) if encrypted else ""


def save_api_key(db, api_key: str):
    db.save_settings({"deepseek_api_key_dpapi": encrypt_for_current_user(api_key)})


def clear_api_key(db):
    db.save_settings({"deepseek_api_key_dpapi": ""})
