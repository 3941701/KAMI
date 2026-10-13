"""Backend selection and the PINE-backed memory adapter.

Every backend offers the same small surface:

    read(addr, size) -> bytes        guest bytes, big-endian memory order
    write(addr, data)                (not for dumps)
    regions() -> [Region]            allocated guest regions
    game_info() -> dict              title / id / version / hash when known
    close()
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import List, Optional

from .common import GUEST_SIZE, VIDEO_START, MemoryError_, Region, merge_regions, parse_int
from .pine import PineBusy, PineClient, PineError, PineFail


class PineMemory:
    """Memory backend over RPCS3's IPC socket.

    Slow for full snapshots (the whole map has to be probed and every read is
    a network round trip) but needs no OS privileges.  Remember that RPCS3
    serves a single IPC client: this cannot run next to KAMI.
    """

    name = "pine"
    writable = True
    PROBE_UNIT = 0x10000        # 64 KiB: allocation granularity of user memory

    def __init__(self, target: Optional[str] = None, timeout: float = 3.0,
                 probe_lo: int = 0x10000, probe_hi: int = VIDEO_START,
                 cache_file: Optional[str] = None):
        self.client = PineClient(target, timeout).connect()
        self.probe_lo, self.probe_hi = probe_lo, probe_hi
        self.cache_file = cache_file
        self._regions: Optional[List[Region]] = None

    def read(self, addr: int, size: int) -> bytes:
        return self.client.read_block(addr, size)

    def write(self, addr: int, data: bytes) -> None:
        self.client.write_block(addr, data)

    def read_u32_many(self, addrs):
        """Raw big-endian words at scattered addresses (numpy '>u4'); NaN pattern if unmapped."""
        import numpy as np
        out = np.full(len(addrs), 0x7FC00000, dtype=">u4")

        def fetch(lo: int, hi: int) -> None:
            if hi <= lo:
                return
            try:
                out[lo:hi] = self.client.read_many([int(a) for a in addrs[lo:hi]], 4)
            except PineFail:
                if hi - lo == 1:
                    return
                mid = (lo + hi) // 2
                fetch(lo, mid)
                fetch(mid, hi)

        fetch(0, len(addrs))
        return out

    # -- region discovery -------------------------------------------------
    def _all_mapped(self, first_unit: int, count: int) -> bool:
        unit = self.PROBE_UNIT
        addrs = [(first_unit + i) * unit for i in range(count)]
        try:
            self.client.read_many(addrs, 1)
            return True
        except PineFail:
            return False

    def _probe(self, first: int, count: int, out: List[int]) -> None:
        """Bisect: a batch of reads succeeds only if every page in it is mapped."""
        if count == 0:
            return
        if self._all_mapped(first, count):
            out.extend(range(first, first + count))
            return
        if count == 1:
            return
        half = count // 2
        self._probe(first, half, out)
        self._probe(first + half, count - half, out)

    def regions(self) -> List[Region]:
        if self._regions is not None:
            return list(self._regions)
        if self.cache_file and os.path.isfile(self.cache_file):
            with open(self.cache_file, "r", encoding="utf-8") as f:
                self._regions = [Region(a, n) for a, n in json.load(f)]
            return list(self._regions)
        unit = self.PROBE_UNIT
        first, last = self.probe_lo // unit, (self.probe_hi + unit - 1) // unit
        mapped: List[int] = []
        chunk = 256
        for u in range(first, last, chunk):
            self._probe(u, min(chunk, last - u), mapped)
        self._regions = merge_regions(Region(u * unit, unit) for u in mapped)
        if self.cache_file:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump([[r.start, r.size] for r in self._regions], f)
        return list(self._regions)

    def game_info(self) -> dict:
        c = self.client
        try:
            return {"emulator": c.version(), "title": c.title(), "id": c.game_id(),
                    "version": c.game_version(), "hash": c.game_uuid(), "status": c.status()}
        except PineError:
            return {}

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------
def add_backend_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("memory source")
    g.add_argument("--backend", choices=("auto", "process", "pine", "dump"), default="auto",
                   help="auto = running rpcs3 process, falling back to PINE (default)")
    g.add_argument("--pid", type=int, help="rpcs3 process id (process backend)")
    g.add_argument("--base", type=parse_int,
                   help="host address of guest memory, normally 0x300000000 (auto-detected)")
    g.add_argument("--pine", metavar="TARGET",
                   help="PINE target: port, tcp:HOST:PORT or unix:/path (default: RPCS3's default)")
    g.add_argument("--dump", metavar="DIR",
                   help="RPCS3 'Dump Guest Memory' folder, or a raw file with --raw-base")
    g.add_argument("--raw-base", type=parse_int, help="guest address of a raw dump file")


def open_backend(args, need_write: bool = False):
    """Open the memory source selected on the command line."""
    choice = args.backend
    if getattr(args, "dump", None):
        choice = "dump"
    if choice == "dump":
        if need_write:
            raise MemoryError_("this command writes memory and cannot work on a dump")
        from .dump import DumpMemory
        if not args.dump:
            raise MemoryError_("--backend dump needs --dump DIR")
        return DumpMemory(args.dump, args.raw_base)
    if choice in ("auto", "process"):
        from .procmem import ProcessError, ProcessMemory
        try:
            return ProcessMemory(args.pid, args.base, writable=True)
        except (ProcessError, OSError) as e:
            if choice == "process":
                raise
            print(f"[backend] process memory unavailable ({e}); trying PINE", file=sys.stderr)
    return PineMemory(args.pine)


def pine_info(target: Optional[str] = None) -> dict:
    """Game identity through PINE, or {} when IPC is off or owned by another client."""
    try:
        with PineClient(target, timeout=1.0) as c:
            return {"emulator": c.version(), "title": c.title(), "id": c.game_id(),
                    "version": c.game_version(), "hash": c.game_uuid(), "status": c.status()}
    except PineError:
        return {}


# --------------------------------------------------------------------------
# Typed helpers shared by the tools
# --------------------------------------------------------------------------
def read_u32(mem, addr: int) -> int:
    return struct.unpack(">I", mem.read(addr, 4))[0]


def read_f32(mem, addr: int) -> float:
    return struct.unpack(">f", mem.read(addr, 4))[0]


def write_f32(mem, addr: int, value: float) -> None:
    mem.write(addr, struct.pack(">f", value))


def write_u32(mem, addr: int, value: int) -> None:
    mem.write(addr, struct.pack(">I", value & 0xFFFFFFFF))


def resolve_chain(mem, base: int, offsets) -> Optional[int]:
    """KAMI DerefChain semantics: value0 = base; value[i] = read32(value[i-1]) + off[i].

    Returns the final address, or None when a hop reads an unmapped address.
    """
    cur = base
    for off in offsets:
        try:
            ptr = read_u32(mem, cur)
        except MemoryError_:
            return None
        if ptr == 0:
            return None
        cur = (ptr + off) & 0xFFFFFFFF
    return cur
