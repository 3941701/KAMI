"""PINE (RPCS3 "IPC") client.

Wire format, as implemented by RPCS3's 3rdparty/pine/pine_server.h:

    request : u32le total_size (including these 4 bytes), then commands
              command = u8 opcode [, u32le address [, value]]
    reply   : u32le total_size, u8 result (0x00 ok / 0xFF fail), payload

Facts that shape this client (all read from the RPCS3 source):

* The server serves ONE client at a time.  A second client connects (it sits
  in the listen backlog) but gets no answers until the first disconnects.
  KAMI holds its connection for as long as it runs, so close KAMI before
  using this backend - or use the direct process-memory backend instead.
* Values travel as host little-endian integers holding the *logical* value;
  the server does the big-endian conversion.  A float is read as u32 and
  reinterpreted, exactly like KAMI's IPCUtils.ReadFloat.
* One unmapped address fails the whole batch with a bare 5-byte error reply.
* MsgStatus consumes 4 phantom argument bytes on the server, so it must be
  the last command of a packet.  This client always sends it alone.
* Limits: request <= 650000 bytes, reply < 450000 bytes.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
from typing import List, Optional, Sequence

from .common import MemoryError_

DEFAULT_SLOT = 28012

MSG_READ8, MSG_READ16, MSG_READ32, MSG_READ64 = 0, 1, 2, 3
MSG_WRITE8, MSG_WRITE16, MSG_WRITE32, MSG_WRITE64 = 4, 5, 6, 7
MSG_VERSION = 8
MSG_TITLE, MSG_ID, MSG_UUID, MSG_GAME_VERSION, MSG_STATUS = 0xB, 0xC, 0xD, 0xE, 0xF

MAX_IPC_SIZE = 650000
MAX_IPC_RETURN_SIZE = 450000

STATUS_NAMES = {0: "Running", 1: "Paused", 2: "Shutdown"}

_READ_OP = {1: MSG_READ8, 2: MSG_READ16, 4: MSG_READ32, 8: MSG_READ64}
_WRITE_OP = {1: MSG_WRITE8, 2: MSG_WRITE16, 4: MSG_WRITE32, 8: MSG_WRITE64}
_FMT = {1: "B", 2: "H", 4: "I", 8: "Q"}


class PineError(MemoryError_):
    pass


class PineBusy(PineError):
    """Connected, but the server never answered: another client owns it."""


class PineFail(PineError):
    """The server answered IPC_FAIL: unmapped/unwritable address or bad command."""


def default_unix_path(slot: int = DEFAULT_SLOT) -> str:
    env = "TMPDIR" if sys.platform == "darwin" else "XDG_RUNTIME_DIR"
    base = os.environ.get(env)
    path = (base.rstrip("/") + "/rpcs3.sock") if base else "/tmp/rpcs3.sock"
    if slot != DEFAULT_SLOT:
        path += f".{slot}"
    return path


class PineClient:
    """Minimal synchronous PINE client.

    target: None (platform default), "tcp:HOST:PORT", "unix:/path", or a bare
    port number.  On Windows RPCS3 listens on 127.0.0.1:<port>; elsewhere on
    a unix socket named rpcs3.sock.
    """

    def __init__(self, target: Optional[str] = None, timeout: float = 3.0):
        self.target = target
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    # -- connection -------------------------------------------------------
    def connect(self) -> "PineClient":
        t = self.target
        if t is None:
            t = f"tcp:127.0.0.1:{DEFAULT_SLOT}" if os.name == "nt" else "unix:" + default_unix_path()
        elif str(t).isdigit():
            t = f"tcp:127.0.0.1:{t}" if os.name == "nt" else "unix:" + default_unix_path(int(t))
        try:
            if t.startswith("tcp:"):
                host, port = t[4:].rsplit(":", 1)
                s = socket.create_connection((host, int(port)), timeout=self.timeout)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            elif t.startswith("unix:"):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(self.timeout)
                s.connect(t[5:])
            else:
                raise PineError(f"bad PINE target {t!r}")
        except OSError as e:
            raise PineError(
                f"cannot connect to RPCS3 IPC at {t}: {e}. Enable it in RPCS3 "
                f"(Manage > Network Services > IPC; Configuration > IPC in older builds) "
                f"and boot a game.") from e
        s.settimeout(self.timeout)
        self.sock = s
        self.target = t
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "PineClient":
        return self.connect() if self.sock is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport --------------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout as e:
                raise PineBusy(
                    "RPCS3 IPC accepted the connection but does not answer. It serves one "
                    "client at a time: close KAMI (or any other PINE client) and retry, "
                    "or use the direct process-memory backend.") from e
            if not chunk:
                raise PineError("RPCS3 IPC closed the connection")
            buf += chunk
        return bytes(buf)

    def _roundtrip(self, payload: bytes) -> bytes:
        if self.sock is None:
            self.connect()
        if len(payload) + 4 > MAX_IPC_SIZE:
            raise PineError("PINE request too large")
        self.sock.sendall(struct.pack("<I", len(payload) + 4) + payload)
        size = struct.unpack("<I", self._recv_exact(4))[0]
        if size < 5 or size > MAX_IPC_RETURN_SIZE:
            raise PineError(f"bad PINE reply size {size}")
        body = self._recv_exact(size - 4)
        if body[0] != 0:
            raise PineFail("IPC_FAIL (unmapped / unwritable address, or bad command)")
        return body[1:]

    # -- info -------------------------------------------------------------
    def _string(self, op: int) -> str:
        body = self._roundtrip(bytes([op]))
        n = struct.unpack_from("<I", body, 0)[0]
        return body[4:4 + n].split(b"\0", 1)[0].decode("utf-8", "replace")

    def version(self) -> str:
        return self._string(MSG_VERSION)

    def title(self) -> str:
        return self._string(MSG_TITLE)

    def game_id(self) -> str:
        return self._string(MSG_ID)

    def game_uuid(self) -> str:
        """Executable hash, e.g. 'PPU-412a9bf3...' (Emu.GetExecutableHash)."""
        return self._string(MSG_UUID)

    def game_version(self) -> str:
        return self._string(MSG_GAME_VERSION)

    def status(self) -> int:
        return struct.unpack_from("<I", self._roundtrip(bytes([MSG_STATUS])), 0)[0]

    # -- scalar access ----------------------------------------------------
    def read(self, addr: int, size: int = 4) -> int:
        body = self._roundtrip(struct.pack("<BI", _READ_OP[size], addr & 0xFFFFFFFF))
        return struct.unpack_from("<" + _FMT[size], body, 0)[0]

    def write(self, addr: int, value: int, size: int = 4) -> None:
        mask = (1 << (8 * size)) - 1
        self._roundtrip(struct.pack("<BI" + _FMT[size], _WRITE_OP[size], addr & 0xFFFFFFFF, value & mask))

    def read_float(self, addr: int) -> float:
        return struct.unpack("<f", struct.pack("<I", self.read(addr, 4)))[0]

    def write_float(self, addr: int, value: float) -> None:
        self.write(addr, struct.unpack("<I", struct.pack("<f", value))[0], 4)

    # -- batched access ---------------------------------------------------
    @staticmethod
    def _max_batch(size: int) -> int:
        by_request = (MAX_IPC_SIZE - 4) // 5
        by_reply = (MAX_IPC_RETURN_SIZE - 6) // size
        return min(by_request, by_reply)

    def read_many(self, addrs: Sequence[int], size: int = 4) -> List[int]:
        """Read many values of one width; raises PineError if any address is unmapped."""
        out: List[int] = []
        step = self._max_batch(size)
        op = _READ_OP[size]
        for i in range(0, len(addrs), step):
            chunk = addrs[i:i + step]
            payload = b"".join(struct.pack("<BI", op, a & 0xFFFFFFFF) for a in chunk)
            body = self._roundtrip(payload)
            out.extend(struct.unpack_from("<%d%s" % (len(chunk), _FMT[size]), body, 0))
        return out

    def write_many(self, items: Sequence[tuple], size: int = 4) -> None:
        """items: (addr, value) pairs, sent in as few packets as possible."""
        per = 5 + size
        step = (MAX_IPC_SIZE - 4) // per
        op = _WRITE_OP[size]
        mask = (1 << (8 * size)) - 1
        for i in range(0, len(items), step):
            payload = b"".join(struct.pack("<BI" + _FMT[size], op, a & 0xFFFFFFFF, v & mask)
                               for a, v in items[i:i + step])
            self._roundtrip(payload)

    def read_block(self, addr: int, size: int) -> bytes:
        """Raw guest bytes (big-endian memory order) for [addr, addr+size)."""
        if size <= 0:
            return b""
        out = bytearray()
        end = addr + size
        cur = addr
        # unaligned head
        while cur < end and cur % 8:
            out.append(self.read(cur, 1))
            cur += 1
        n8 = (end - cur) // 8
        step = self._max_batch(8)
        i = 0
        while i < n8:
            cnt = min(step, n8 - i)
            base = cur + i * 8
            payload = b"".join(struct.pack("<BI", MSG_READ64, (base + 8 * k) & 0xFFFFFFFF)
                               for k in range(cnt))
            body = self._roundtrip(payload)
            # each value is the logical u64 sent little-endian; reversing every
            # 8-byte group restores guest (big-endian) byte order
            raw = memoryview(body)[:cnt * 8]
            try:
                import numpy as np
                out += np.frombuffer(raw, dtype="<u8").astype(">u8").tobytes()
            except ImportError:  # pragma: no cover - numpy is a hard dependency of the tools
                for k in range(cnt):
                    out += bytes(raw[8 * k:8 * k + 8])[::-1]
            i += cnt
        cur += n8 * 8
        while cur < end:
            out.append(self.read(cur, 1))
            cur += 1
        return bytes(out)

    def write_block(self, addr: int, data: bytes) -> None:
        items = [(addr + i, b) for i, b in enumerate(data)]
        if len(data) % 4 == 0 and addr % 4 == 0:
            items = [(addr + i, struct.unpack_from(">I", data, i)[0]) for i in range(0, len(data), 4)]
            self.write_many(items, 4)
        else:
            self.write_many(items, 1)

    def is_mapped(self, addr: int) -> bool:
        try:
            self.read(addr, 1)
            return True
        except PineFail:
            return False
