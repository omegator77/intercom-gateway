"""UDP sockets for SIP and RTP."""
from __future__ import annotations

import socket
import sys

SIO_UDP_CONNRESET = 0x9800000C  # _WSAIOW(IOC_VENDOR, 12); the constant is not exported by Python's socket module


def udp_socket(ip: str, port: int) -> socket.socket:
    """A bound, non-blocking IPv4 UDP socket. On Windows a datagram sent to a port nobody listens on makes the
    *next receive* of the sending socket fail with 'connection reset' (WSAECONNRESET), which stops asyncio from
    reading at all - fatal for a codec that is restarting while we keep sending. That behaviour is switched off."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform == "win32":
            _no_connreset(s)
        s.bind((ip, port))
        s.setblocking(False)
    except OSError:
        s.close()
        raise
    return s


def _no_connreset(s: socket.socket) -> None:
    """WSAIoctl(SIO_UDP_CONNRESET, FALSE); Python's own `ioctl` only knows a few other commands."""
    import ctypes
    from ctypes import wintypes

    flag = wintypes.DWORD(0)
    returned = wintypes.DWORD(0)
    rc = ctypes.windll.ws2_32.WSAIoctl(  # type: ignore[attr-defined]
        ctypes.c_size_t(s.fileno()), wintypes.DWORD(SIO_UDP_CONNRESET), ctypes.byref(flag), wintypes.DWORD(4), None,
        wintypes.DWORD(0), ctypes.byref(returned), None, None)
    if rc != 0:
        raise OSError(ctypes.WinError())  # type: ignore[attr-defined]
