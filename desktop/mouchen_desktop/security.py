from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol


class Protector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class IdentityProtector:
    """Test-only protector used with disposable data."""

    def protect(self, value: bytes) -> bytes:
        return value

    def unprotect(self, value: bytes) -> bytes:
        return value


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDataProtector:
    """Encrypts local values with the current Windows user's DPAPI key."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, entropy: bytes = b"mouchen-desktop-v1") -> None:
        if os.name != "nt":
            raise RuntimeError("Windows DPAPI is required for persistent private-alpha data")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        self._entropy = entropy
        self._configure_api()

    def protect(self, value: bytes) -> bytes:
        return self._transform("CryptProtectData", value)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform("CryptUnprotectData", value)

    def _configure_api(self) -> None:
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    def _transform(self, operation: str, value: bytes) -> bytes:
        input_blob, input_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        function = getattr(self._crypt32, operation)
        if operation == "CryptProtectData":
            success = function(
                ctypes.byref(input_blob),
                "My AI Twin Desktop",
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            success = function(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        # Keep input buffers alive until the native call has completed.
        _ = input_buffer, entropy_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
            if description:
                self._kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(value, max(1, len(value)))
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return _DataBlob(len(value), pointer), buffer
