"""Persistent account-level WFP filters, including loopback and IPv6.

Definitions follow the Windows SDK fwpmtypes.h/fwptypes.h ABI. This trusted
module can also run from the sandbox setup directory without Wright imports.
"""

import ctypes as c
from ctypes import wintypes as w
from uuid import UUID, uuid4

try:
    import wright.infrastructure.runtime.windows_native as native
except ImportError:
    import windows_native as native


class GUID(c.Structure):
    _fields_ = [("first", c.c_uint32), ("second", c.c_uint16), ("third", c.c_uint16), ("tail", c.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(UUID(str(value)).bytes_le)


class DISPLAY(c.Structure):
    _fields_ = [("name", w.LPWSTR), ("description", w.LPWSTR)]


class UNION(c.Union):
    _fields_ = [("pointer", c.c_void_p), ("uint8", c.c_uint8), ("uint32", c.c_uint32)]


class VALUE(c.Structure):
    _fields_ = [("type", c.c_int), ("value", UNION)]


class CONDITION(c.Structure):
    _fields_ = [("key", GUID), ("match", c.c_int), ("value", VALUE)]


class ACTION(c.Structure):
    _fields_ = [("type", c.c_uint32), ("key", GUID)]


class CONTEXT(c.Union):
    _fields_ = [("raw", c.c_uint64), ("key", GUID)]


class FILTER(c.Structure):
    _fields_ = [("key", GUID), ("display", DISPLAY), ("flags", c.c_uint32),
               ("provider", c.POINTER(GUID)), ("data", native.BLOB), ("layer", GUID),
               ("sublayer", GUID), ("weight", VALUE), ("count", c.c_uint32),
               ("conditions", c.POINTER(CONDITION)), ("action", ACTION), ("context", CONTEXT),
               ("reserved", c.POINTER(GUID)), ("id", c.c_uint64), ("effective", VALUE)]


class SUBLAYER(c.Structure):
    _fields_ = [("key", GUID), ("display", DISPLAY), ("flags", c.c_uint32),
               ("provider", c.POINTER(GUID)), ("data", native.BLOB), ("weight", c.c_uint16)]


_LAYERS = ("c38d57d1-05a7-4c33-904f-7fbceee60e82", "4a72393b-319f-44bc-84c3-ba54dcb3b6b4",
           "e1cd9fe7-f4b5-4273-96c0-592e487b8650", "a3b42c97-9f04-4672-b87e-cee9c483257f")
_USER = "af043a0a-b34d-4f86-979c-c90371af6e66"
api = c.WinDLL("fwpuclnt", use_last_error=True)


def check(error):
    if error:
        raise OSError(error, "Windows Filtering Platform operation failed")


def engine():
    handle = w.HANDLE()
    function = native.bind(api, "FwpmEngineOpen0", [w.LPCWSTR, w.DWORD, c.c_void_p, c.c_void_p, c.POINTER(w.HANDLE)], w.DWORD)
    check(function(None, 10, None, None, c.byref(handle)))
    return handle


def close(handle):
    check(native.bind(api, "FwpmEngineClose0", [w.HANDLE], w.DWORD)(handle))


def identities() -> dict:
    return {"sublayer": str(uuid4()), "filters": [str(uuid4()) for _ in _LAYERS]}


def check_removal(error):
    # Idempotent recovery of a journaled transaction that never committed.
    if error not in (0, 0x80320003, 0x80320007):
        check(error)


def install(identity: str, keys_state: dict) -> dict:
    handle = engine()
    descriptor = c.c_void_p()
    subkey = keys_state["sublayer"]
    keys = keys_state["filters"]
    begin = native.bind(api, "FwpmTransactionBegin0", [w.HANDLE, w.DWORD], w.DWORD)
    commit = native.bind(api, "FwpmTransactionCommit0", [w.HANDLE], w.DWORD)
    abort = native.bind(api, "FwpmTransactionAbort0", [w.HANDLE], w.DWORD)
    try:
        check(begin(handle, 0))
        sub = SUBLAYER()
        sub.key, sub.display, sub.flags, sub.weight = GUID.parse(subkey), DISPLAY("Wright offline commands", None), 1, 0xFFFF
        add_sub = native.bind(api, "FwpmSubLayerAdd0", [w.HANDLE, c.POINTER(SUBLAYER), c.c_void_p], w.DWORD)
        check(add_sub(handle, c.byref(sub), None))
        convert = native.bind(native.security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)])
        size = w.DWORD()
        native.check(convert(f"D:(A;;CC;;;{identity})", 1, c.byref(descriptor), c.byref(size)))
        blob = native.BLOB(size, c.cast(descriptor, c.POINTER(c.c_ubyte)))
        condition = CONDITION(GUID.parse(_USER), 0, VALUE(14, UNION(pointer=c.addressof(blob))))
        add = native.bind(api, "FwpmFilterAdd0", [w.HANDLE, c.POINTER(FILTER), c.c_void_p, c.POINTER(c.c_uint64)], w.DWORD)
        for key, layer in zip(keys, _LAYERS, strict=True):
            item = FILTER()
            item.key, item.display, item.flags = GUID.parse(key), DISPLAY("Wright offline account", None), 1
            item.layer, item.sublayer = GUID.parse(layer), GUID.parse(subkey)
            item.weight = VALUE(1, UNION(uint8=15))
            item.count, item.conditions, item.action = 1, c.pointer(condition), ACTION(0x1001, GUID())
            check(add(handle, c.byref(item), None, None))
        check(commit(handle))
        return {"sublayer": subkey, "filters": keys}
    except Exception:
        abort(handle)
        raise
    finally:
        if descriptor:
            native.local_free(descriptor)
        close(handle)


def verify(state: dict, identity: str | None = None) -> None:
    if len(state.get("filters", [])) != 4:
        raise RuntimeError("Offline network isolation is incomplete")
    handle = engine()
    get = native.bind(api, "FwpmFilterGetByKey0", [w.HANDLE, c.POINTER(GUID), c.POINTER(c.POINTER(FILTER))], w.DWORD)
    free = native.bind(api, "FwpmFreeMemory0", [c.c_void_p], None)
    try:
        for key, layer in zip(state["filters"], _LAYERS, strict=True):
            item = c.POINTER(FILTER)()
            check(get(handle, c.byref(GUID.parse(key)), c.byref(item)))
            try:
                value = item.contents
                if bytes(value.layer) != UUID(layer).bytes_le or bytes(value.sublayer) != UUID(state["sublayer"]).bytes_le or value.count != 1:
                    raise RuntimeError("Offline network filter scope changed")
                condition = value.conditions[0]
                if bytes(condition.key) != UUID(_USER).bytes_le or condition.match != 0 or condition.value.type != 14:
                    raise RuntimeError("Offline network filter identity changed")
                if identity:
                    blob = c.cast(condition.value.value.pointer, c.POINTER(native.BLOB)).contents
                    descriptor = w.LPWSTR()
                    convert = native.bind(native.security, "ConvertSecurityDescriptorToStringSecurityDescriptorW", [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p])
                    native.check(convert(blob.data, 1, 4, c.byref(descriptor), None))
                    try:
                        if descriptor.value != f"D:(A;;CC;;;{identity})":
                            raise RuntimeError("Offline network filter account changed")
                    finally:
                        native.local_free(descriptor)
                if item.contents.action.type != 0x1001 or not item.contents.flags & 1:
                    raise RuntimeError("Offline network filter changed")
            finally:
                free(c.byref(item))
    finally:
        close(handle)


def remove(state: dict) -> None:
    handle = engine()
    delete = native.bind(api, "FwpmFilterDeleteByKey0", [w.HANDLE, c.POINTER(GUID)], w.DWORD)
    try:
        for key in state.get("filters", []):
            check_removal(delete(handle, c.byref(GUID.parse(key))))
        sub = native.bind(api, "FwpmSubLayerDeleteByKey0", [w.HANDLE, c.POINTER(GUID)], w.DWORD)
        if state.get("sublayer"):
            check_removal(sub(handle, c.byref(GUID.parse(state["sublayer"]))))
    finally:
        close(handle)
