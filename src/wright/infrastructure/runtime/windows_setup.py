"""Elevated, standalone bootstrap for the protected Windows sandbox runtime."""

import ctypes as c
import json
import os
import sys
from pathlib import Path

import windows_native as native
import windows_wfp as wfp


class USER_INFO(c.Structure):
    _fields_ = [("name", native.w.LPWSTR), ("password", native.w.LPWSTR), ("age", native.w.DWORD),
               ("privilege", native.w.DWORD), ("home", native.w.LPWSTR), ("comment", native.w.LPWSTR),
               ("flags", native.w.DWORD), ("script", native.w.LPWSTR)]


def create_account(name, password):
    net = c.WinDLL("netapi32")
    add = native.bind(net, "NetUserAdd", [native.w.LPCWSTR, native.w.DWORD, c.c_void_p, c.POINTER(native.w.DWORD)], native.w.DWORD)
    user = USER_INFO(name, password, 0, 1, None, "Wright isolated command execution", 0x10000 | 0x200, None)
    error = add(None, 1, c.byref(user), None)
    if error == 2224:
        # Recover a crash between creation and SID journaling only by proving
        # possession of the random, protected password. Never reset a collision.
        token = native.w.HANDLE()
        logon = native.bind(native.security, "LogonUserW", [native.w.LPCWSTR, native.w.LPCWSTR, native.w.LPCWSTR,
                           native.w.DWORD, native.w.DWORD, c.POINTER(native.w.HANDLE)])
        if not logon(name, ".", password, 2, 0, c.byref(token)):
            raise RuntimeError("A sandbox account name is already owned by another account")
        native.close(token)
    elif error:
        raise c.WinError(error)
    return account_sid(name)


def account_sid(name):
    # Resolve the SID without printing credentials or depending on localized groups.
    lookup = native.bind(native.security, "LookupAccountNameW", [native.w.LPCWSTR, native.w.LPCWSTR, c.c_void_p,
                         c.POINTER(native.w.DWORD), native.w.LPWSTR, c.POINTER(native.w.DWORD), c.POINTER(native.w.DWORD)])
    sid_size, domain_size, use = native.w.DWORD(), native.w.DWORD(), native.w.DWORD()
    lookup(None, name, None, c.byref(sid_size), None, c.byref(domain_size), c.byref(use))
    sid, domain = c.create_string_buffer(sid_size.value), c.create_unicode_buffer(domain_size.value)
    native.check(lookup(None, name, sid, c.byref(sid_size), domain, c.byref(domain_size), c.byref(use)))
    output = native.w.LPWSTR()
    convert = native.bind(native.security, "ConvertSidToStringSidW", [c.c_void_p, c.POINTER(native.w.LPWSTR)])
    native.check(convert(sid, c.byref(output)))
    try:
        return output.value
    finally:
        native.local_free(output)


def save(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def main(root: Path, action: str):
    config_path = root / "state.json"
    state = json.loads(config_path.read_text(encoding="utf-8"))
    state.update(bootstrap_pid=os.getpid(), bootstrap_started=native.process_identity(os.getpid()))
    save(config_path, state)
    if action == "cleanup":
        if state.get("network"):
            wfp.remove(state["network"])
            state.pop("network")
            save(config_path, state)
        for entry in list(state.get("acl_journal", [])):
            if entry.get("registry") or Path(entry["path"]).exists():
                native.acl(entry["path"], entry["sid"], 0, remove=True, registry=entry.get("registry", False))
            state["acl_journal"].remove(entry)
            save(config_path, state)
        net = c.WinDLL("netapi32")
        delete = native.bind(net, "NetUserDel", [native.w.LPCWSTR, native.w.LPCWSTR], native.w.DWORD)
        for kind, sid in list(state.get("account_sids", {}).items()):
            name = state["accounts"][kind]
            try:
                current = account_sid(name)
            except OSError as error:
                if error.winerror != 1332:
                    raise
                current = None  # A prior cleanup may have deleted it before journaling.
            if current is not None:
                if current != sid:
                    raise RuntimeError("Sandbox account identity changed; refusing to delete another account")
                error = delete(None, name)
                if error not in (0, 2221):
                    raise c.WinError(error)
            del state["account_sids"][kind]
            save(config_path, state)
        state.pop("accounts", None)
        state["state"] = "setup_required"
        (root / "credentials.bin").unlink(missing_ok=True)
    else:
        secrets = json.loads(native.protect((root / "credentials.bin").read_bytes(), decrypt=True))
        for kind, name in state["accounts"].items():
            known = state.setdefault("account_sids", {}).get(kind)
            if known:
                if account_sid(name) != known:
                    raise RuntimeError("Sandbox account identity changed")
            else:
                state["account_sids"][kind] = create_account(name, secrets[kind])
                save(config_path, state)
        def grant(path, sid, mask, registry=False, deny=False):
            entry = {"path": path, "sid": sid, "registry": registry}
            if entry not in state.setdefault("acl_journal", []):
                state["acl_journal"].append(entry)
                save(config_path, state)
            native.acl(path, sid, mask, registry=registry, deny=deny)
        for path in state["runtime_roots"]:
            grant(path, state["runtime_sid"], 0xD0156, deny=True)
            for sid in (state["runtime_sid"], *state["account_sids"].values()):
                grant(path, sid, 0x1200A9)
        for path in state.get("protected_runtime_paths", []):
            grant(path, state["runtime_sid"], 0x1F01FF, deny=True)
        for path in ("MACHINE\\SOFTWARE", "MACHINE\\SYSTEM"):
            grant(path, state["runtime_sid"], 0x20019, registry=True)
        for file in ("windows_runner.py", "windows_native.py"):
            for sid in state["account_sids"].values():
                grant(str(root / file), sid, 0x1200A9)
        if not state.get("network"):
            state["network"] = wfp.identities()
            save(config_path, state)
        try:
            wfp.verify(state["network"], state["account_sids"]["offline"])
        except Exception:
            wfp.remove(state["network"])
            wfp.install(state["account_sids"]["offline"], state["network"])
        wfp.verify(state["network"], state["account_sids"]["offline"])
        state["state"] = "ready"
    state.pop("error", None)
    save(config_path, state)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    try:
        main(root, sys.argv[2])
    except Exception as error:
        path = root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(state="failed", error=f"{type(error).__name__}: {error}")
        save(path, state)
        sys.exit(1)
