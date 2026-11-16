"""Linux sandbox entry: bridge the egress proxy, confine, then exec the shell.

Runs inside Bubblewrap's private network namespace, where only ``lo``
exists. It is copied into the command's scratch directory and started with
the trusted interpreter, so it imports nothing from Wright:

    python -I -S launcher.py PORT SOCKET FILTER -- /bin/bash ...

1. listen on 127.0.0.1:PORT inside the namespace;
2. fork a forwarder that relays each connection to the host proxy's Unix
   SOCKET. The forwarder is non-dumpable and dies with the shell;
3. install the seccomp FILTER on this process (new sockets: AF_INET and
   AF_INET6 only, which here reach only ``lo``; no ptrace, no
   process_vm_*, no io_uring) and exec the command.

Any setup failure exits before the command runs.
"""

import ctypes
import os
import select
import signal
import socket
import sys
import threading

PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_SET_SECCOMP = 22
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_MODE_FILTER = 2


def _relay(client, socket_path):
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(socket_path)
        pair = [client, upstream]
        while True:
            readable, _, broken = select.select(pair, [], pair)
            if broken:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    except OSError:
        return
    finally:
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass


def _forward(listener, socket_path):
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_relay, args=(client, socket_path), daemon=True).start()


def _fail(message):
    os.write(2, f"wright sandbox: {message}\n".encode())
    os._exit(126)


def main(argv):
    if "--" not in argv:
        _fail("launcher usage error")
    separator = argv.index("--")
    port, socket_path, filter_path = int(argv[0]), argv[1], argv[2]
    command = argv[separator + 1:]
    libc = ctypes.CDLL(None, use_errno=True)
    # prctl takes unsigned long arguments; untyped ints could carry garbage
    # high bits, which the kernel rejects for PR_SET_NO_NEW_PRIVS.
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    with open(filter_path, "rb") as handle:
        program = handle.read()
    if not program or len(program) % 8:
        _fail("invalid seccomp program")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    parent = os.getpid()
    child = os.fork()
    if child == 0:
        try:
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
            libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
            if os.getppid() != parent:
                os._exit(0)
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null, descriptor)
            _forward(listener, socket_path)
        finally:
            os._exit(0)
    listener.close()
    buffer = ctypes.create_string_buffer(program)

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = SockFprog(len(program) // 8, ctypes.cast(buffer, ctypes.c_void_p))
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _fail("could not set no_new_privs")
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0) != 0:
        _fail(f"could not install seccomp filter (errno {ctypes.get_errno()})")
    try:
        os.execv(command[0], command)
    except OSError as error:
        _fail(f"could not start {command[0]}: {error}")


if __name__ == "__main__":
    main(sys.argv[1:])
