"""Payload for the Linux network boundary's actual host-listener checks.

The enclosing native test supplies a socket pathname and exposes its parent
read-only, so failures cannot be credited to a hidden socket pathname.
"""

import ctypes
import errno
import os
import socket
from pathlib import Path

libc = ctypes.CDLL(None, use_errno=True)
libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
libc.connect.restype = ctypes.c_int


def rejected(expected, operation, *args):
    try:
        operation(*args)
    except OSError as error:
        assert error.errno == expected, (expected, error)
    else:
        raise AssertionError("socket operation unexpectedly succeeded")


def verify_socketpairs(host_socket_path):
    for family, kind, protocol in (
        (socket.AF_UNIX, socket.SOCK_DGRAM, 0),
        (socket.AF_UNIX, socket.SOCK_SEQPACKET, 0),
        (socket.AF_UNIX, socket.SOCK_STREAM | (1 << 30), 0),
        (socket.AF_UNIX, socket.SOCK_STREAM, 1),
        (socket.AF_INET, socket.SOCK_STREAM, 0),
    ):
        rejected(errno.EPERM, socket.socketpair, family, kind, protocol)

    assert Path(host_socket_path).is_socket()
    for flags in (0, socket.SOCK_CLOEXEC, socket.SOCK_NONBLOCK, socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM | flags)
        try:
            left.send(b"private pair")
            assert right.recv(12) == b"private pair"
            rejected(errno.EISCONN, left.connect, host_socket_path)
            address = (ctypes.c_ushort * 8)()  # AF_UNSPEC disconnects datagram pairs only.
            assert libc.connect(left.fileno(), ctypes.byref(address), ctypes.sizeof(address)) == -1
            assert ctypes.get_errno() == errno.EINVAL
            rejected(errno.EISCONN, left.sendto, b"escape", host_socket_path)
            left.bind(str(Path(os.environ["TMPDIR"]) / f"pair-{flags}.sock"))
            rejected(errno.EINVAL, left.listen)
            left.shutdown(socket.SHUT_RDWR)
            rejected(errno.EISCONN, left.connect, host_socket_path)
            right.close()
            rejected(errno.EISCONN, left.connect, host_socket_path)
            rejected(errno.EINVAL, left.listen)
        finally:
            left.close()
            right.close()
    print("private stream pairs cannot reconnect or listen")
