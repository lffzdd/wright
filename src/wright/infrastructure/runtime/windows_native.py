"""Small Win32 binding shared with the trusted Windows command runner.

This file deliberately has no Wright or third-party imports. The runner is
copied to protected runtime storage and executed with the base interpreter.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w

kernel = c.WinDLL("kernel32", use_last_error=True)
security = c.WinDLL("advapi32", use_last_error=True)


class SID_ATTRIBUTES(c.Structure):
    _fields_ = [("Sid", c.c_void_p), ("Attributes", w.DWORD)]


class TRUSTEE(c.Structure):
    _fields_ = [("MultipleTrustee", c.c_void_p), ("Operation", c.c_int), ("Form", c.c_int), ("Type", c.c_int), ("Name", c.c_void_p)]


class ACCESS(c.Structure):
    _fields_ = [("Permissions", w.DWORD), ("Mode", c.c_int), ("Inheritance", w.DWORD), ("Trustee", TRUSTEE)]


class STARTUP(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
               ("x", w.DWORD), ("y", w.DWORD), ("xsize", w.DWORD), ("ysize", w.DWORD),
               ("xchars", w.DWORD), ("ychars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
               ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_data", c.c_void_p),
               ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class PROCESS(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


class JOB_LIMIT(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
               ("min_working", c.c_size_t), ("max_working", c.c_size_t), ("active_limit", w.DWORD),
               ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class JOB_EXTENDED(c.Structure):
    _fields_ = [("basic", JOB_LIMIT), ("io", c.c_uint64 * 6), ("process_memory", c.c_size_t),
               ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]


class JOB_ACCOUNTING(c.Structure):
    _fields_ = [("times", c.c_int64 * 4), ("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]


def bind(dll, name, args, result=w.BOOL):
    function = getattr(dll, name)
    function.argtypes = args
    function.restype = result
    return function


close = bind(kernel, "CloseHandle", [w.HANDLE])
resume = bind(kernel, "ResumeThread", [w.HANDLE], w.DWORD)
wait = bind(kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
local_free = bind(kernel, "LocalFree", [c.c_void_p], c.c_void_p)
exit_code = bind(kernel, "GetExitCodeProcess", [w.HANDLE, c.POINTER(w.DWORD)])
convert_sid = bind(security, "ConvertStringSidToSidW", [w.LPCWSTR, c.POINTER(c.c_void_p)])
get_acl = bind(security, "GetNamedSecurityInfoW", [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p,
                                                            c.POINTER(c.c_void_p), c.c_void_p, c.POINTER(c.c_void_p)], w.DWORD)
set_acl = bind(security, "SetNamedSecurityInfoW", [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], w.DWORD)
merge_acl = bind(security, "SetEntriesInAclW", [w.ULONG, c.POINTER(ACCESS), c.c_void_p, c.POINTER(c.c_void_p)], w.DWORD)


def check(value, message="Windows sandbox operation failed"):
    if not value:
        raise c.WinError(c.get_last_error(), message)
    return value


def acl(path: str, identity: str, mask: int, *, deny=False, remove=False, registry=False):
    """Change only this capability's ACE; never restore a stale whole DACL."""
    sid = c.c_void_p()
    check(convert_sid(identity, c.byref(sid)))
    descriptor = c.c_void_p()
    old = c.c_void_p()
    new = c.c_void_p()
    object_type = 4 if registry else 1
    try:
        error = get_acl(path, object_type, 4, None, None, c.byref(old), None, c.byref(descriptor))
        if error:
            raise c.WinError(error)
        entry = ACCESS(mask, 4 if remove else (3 if deny else 1), 3 if not registry else 2,
                       TRUSTEE(None, 0, 0, 1, sid))
        error = merge_acl(1, c.byref(entry), old, c.byref(new))
        if error:
            raise c.WinError(error)
        error = set_acl(path, object_type, 4, None, None, new, None)
        if error:
            raise c.WinError(error)
    finally:
        for pointer in (sid, descriptor, new):
            if pointer:
                local_free(pointer)


def new_job():
    create = bind(kernel, "CreateJobObjectW", [c.c_void_p, w.LPCWSTR], w.HANDLE)
    handle = check(create(None, None))
    limits = JOB_EXTENDED()
    limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; children cannot break away.
    set_info = bind(kernel, "SetInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])
    check(set_info(handle, 9, c.byref(limits), c.sizeof(limits)))
    return handle


def job_active(handle) -> bool:
    query = bind(kernel, "QueryInformationJobObject", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p])
    info = JOB_ACCOUNTING()
    check(query(handle, 1, c.byref(info), c.sizeof(info), None))
    return bool(info.active)


def assign_job(job, process):
    assign = bind(kernel, "AssignProcessToJobObject", [w.HANDLE, w.HANDLE])
    check(assign(job, process))


def terminate_job(job):
    terminate = bind(kernel, "TerminateJobObject", [w.HANDLE, w.UINT])
    check(terminate(job, 1))


def logon_process(username, password, executable, commandline, cwd):
    start = STARTUP()
    start.cb = c.sizeof(start)
    process = PROCESS()
    create = bind(security, "CreateProcessWithLogonW", [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.LPCWSTR,
                 w.LPWSTR, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(STARTUP), c.POINTER(PROCESS)])
    check(create(username, ".", password, 0, executable, c.create_unicode_buffer(commandline),
                 0x08000004, None, cwd, c.byref(start), c.byref(process)))
    return process


def restricted_process(commandline, cwd, environment, identities, owners, output_handle, input_handle):
    open_token = bind(security, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
    current = bind(kernel, "GetCurrentProcess", [], w.HANDLE)
    token = w.HANDLE()
    restricted = w.HANDLE()
    pointers = []
    check(open_token(current(), 0xF01FF, c.byref(token)))
    try:
        for identity in identities:
            sid = c.c_void_p()
            check(convert_sid(identity, c.byref(sid)))
            pointers.append(sid)
        entries = (SID_ATTRIBUTES * len(pointers))(*(SID_ATTRIBUTES(pointer, 4) for pointer in pointers))
        restrict = bind(security, "CreateRestrictedToken", [w.HANDLE, w.DWORD, w.DWORD, c.c_void_p, w.DWORD,
                       c.c_void_p, w.DWORD, c.POINTER(SID_ATTRIBUTES), c.POINTER(w.HANDLE)])
        check(restrict(token, 1, 0, None, 0, None, len(entries), entries, c.byref(restricted)))
        # New files must be accessible in both access-check passes.
        sd = c.c_void_p()
        convert = bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p])
        check(convert("D:" + "".join(f"(A;;FA;;;{identity})" for identity in (identities[-1], *owners)), 1, c.byref(sd), None))
        try:
            present = w.BOOL()
            defaulted = w.BOOL()
            dacl = c.c_void_p()
            get_dacl = bind(security, "GetSecurityDescriptorDacl", [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)])
            check(get_dacl(sd, c.byref(present), c.byref(dacl), c.byref(defaulted)))
            set_token = bind(security, "SetTokenInformation", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD])
            check(set_token(restricted, 6, c.byref(dacl), c.sizeof(dacl)))
        finally:
            local_free(sd)
        start = STARTUP()
        start.cb = c.sizeof(start)
        start.flags = 0x100
        start.stdin = input_handle
        start.stdout = output_handle
        start.stderr = output_handle
        process = PROCESS()
        create = bind(security, "CreateProcessAsUserW", [w.HANDLE, w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p,
                      w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(STARTUP), c.POINTER(PROCESS)])
        block = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0")
        check(create(restricted, None, c.create_unicode_buffer(commandline), None, None, True,
                     0x08000400, block, cwd, c.byref(start), c.byref(process)))
        return process
    finally:
        close(token)
        if restricted:
            close(restricted)
        for pointer in pointers:
            local_free(pointer)


class BLOB(c.Structure):
    _fields_ = [("size", w.DWORD), ("data", c.POINTER(c.c_ubyte))]


def protect(data: bytes, decrypt=False, machine=False) -> bytes:
    crypt = c.WinDLL("crypt32", use_last_error=True)
    source_data = (c.c_ubyte * len(data)).from_buffer_copy(data)
    source = BLOB(len(data), source_data)
    target = BLOB()
    function = bind(crypt, "CryptUnprotectData" if decrypt else "CryptProtectData",
                    [c.POINTER(BLOB), c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, w.DWORD, c.POINTER(BLOB)])
    check(function(c.byref(source), None, None, None, None, 5 if machine and not decrypt else 1, c.byref(target)))
    try:
        return c.string_at(target.data, target.size)
    finally:
        local_free(target.data)


def current_sid() -> str:
    token = w.HANDLE()
    current = bind(kernel, "GetCurrentProcess", [], w.HANDLE)
    open_token = bind(security, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
    check(open_token(current(), 8, c.byref(token)))
    try:
        size = w.DWORD()
        query = bind(security, "GetTokenInformation", [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)])
        query(token, 1, None, 0, c.byref(size))
        data = c.create_string_buffer(size.value)
        check(query(token, 1, data, size, c.byref(size)))
        sid = c.cast(data, c.POINTER(SID_ATTRIBUTES)).contents.Sid
        output = w.LPWSTR()
        convert = bind(security, "ConvertSidToStringSidW", [c.c_void_p, c.POINTER(w.LPWSTR)])
        check(convert(sid, c.byref(output)))
        try:
            return output.value
        finally:
            local_free(output)
    finally:
        close(token)


def private_directory(path: str, owner: str):
    descriptor = c.c_void_p()
    convert = bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p])
    check(convert(f"D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1, c.byref(descriptor), None))
    try:
        present, defaulted, dacl = w.BOOL(), w.BOOL(), c.c_void_p()
        get_dacl = bind(security, "GetSecurityDescriptorDacl", [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)])
        check(get_dacl(descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)))
        error = set_acl(path, 1, 4 | 0x80000000, None, None, dacl, None)
        if error:
            raise c.WinError(error)
    finally:
        local_free(descriptor)


def process_identity(pid):
    open_process = bind(kernel, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    handle = open_process(0x1000, False, pid)
    if not handle:
        return None
    try:
        creation, exited, system, user = (w.FILETIME() for _ in range(4))
        get_times = bind(kernel, "GetProcessTimes", [w.HANDLE, c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME), c.POINTER(w.FILETIME)])
        if not get_times(handle, c.byref(creation), c.byref(exited), c.byref(system), c.byref(user)):
            return None
        return creation.dwHighDateTime << 32 | creation.dwLowDateTime
    finally:
        close(handle)
