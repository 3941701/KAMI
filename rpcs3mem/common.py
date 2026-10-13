"""Shared types and helpers for the RPCS3 memory backends.

All addresses in this package are *guest* (PS3) addresses, 32-bit, unless a
name says ``host``.  PS3 memory is big-endian; helpers here always convert.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

GUEST_SIZE = 0x1_0000_0000          # 4 GiB guest address space
PAGE = 0x1000                       # RPCS3 guest page granularity
MAIN_ELF_BASE = 0x10000             # where PS3 executables are loaded

# RPCS3 reserves 8 GiB at the first free multiple of 4 GiB at or above
# 0x3_0000_0000: [base, base+4G) is the guest view the game itself uses
# (pages can be temporarily read-only / no-access), [base+4G, base+8G) is the
# "sudo" mirror where every allocated page stays read/write.
# (rpcs3/Emu/Memory/vm.cpp: g_base_addr / g_sudo_addr)
HOST_BASE_FIRST = 0x3_0000_0000
HOST_BASE_STEP = 0x1_0000_0000
SUDO_OFFSET = 0x1_0000_0000

# Fixed blocks of the RPCS3 guest map (vm.cpp, ps3_::init)
VIDEO_START = 0xC000_0000           # RSX local memory, 256 MiB, preallocated
STACK_START = 0xD000_0000
SPU_START = 0xE000_0000


class MemoryError_(Exception):
    """Raised when guest memory cannot be read or written."""


@dataclass(frozen=True)
class Region:
    start: int
    size: int
    writable: bool = True
    executable: bool = False

    @property
    def end(self) -> int:
        return self.start + self.size

    def __contains__(self, addr: int) -> bool:
        return self.start <= addr < self.end

    def __str__(self) -> str:
        flags = ("r" + ("w" if self.writable else "-") + ("x" if self.executable else "-"))
        return f"{self.start:08X}-{self.end:08X} {flags} {self.size / 1048576:8.2f} MiB"


def merge_regions(regions: Iterable[Region]) -> List[Region]:
    """Coalesce adjacent regions that share the same flags."""
    out: List[Region] = []
    for r in sorted(regions, key=lambda r: r.start):
        if r.size <= 0:
            continue
        if out and out[-1].end == r.start and out[-1].writable == r.writable \
                and out[-1].executable == r.executable:
            out[-1] = Region(out[-1].start, out[-1].size + r.size, r.writable, r.executable)
        else:
            out.append(r)
    return out


def clip_regions(regions: Sequence[Region], lo: int, hi: int) -> List[Region]:
    """Restrict regions to the half-open guest range [lo, hi)."""
    out = []
    for r in regions:
        s, e = max(r.start, lo), min(r.end, hi)
        if e > s:
            out.append(Region(s, e - s, r.writable, r.executable))
    return out


def parse_int(text) -> int:
    """Accept 0x-prefixed hex, bare hex with a-f digits, or decimal."""
    if isinstance(text, int):
        return text
    t = str(text).strip().replace("_", "")
    if t.lower().startswith("0x"):
        return int(t, 16)
    try:
        return int(t, 10)
    except ValueError:
        return int(t, 16)


def parse_range(text: str) -> Tuple[int, int]:
    """'0x10000-0x40000000' or '0x10000:0x40000000' -> (lo, hi)."""
    for sep in ("-", ":", ".."):
        if sep in text:
            a, b = text.split(sep, 1)
            return parse_int(a), parse_int(b)
    raise ValueError(f"bad range {text!r}; expected LO-HI")


def be_f32(data: bytes, off: int = 0) -> float:
    return struct.unpack_from(">f", data, off)[0]


def be_u32(data: bytes, off: int = 0) -> int:
    return struct.unpack_from(">I", data, off)[0]


def pack_f32(value: float) -> bytes:
    return struct.pack(">f", value)


def pack_u32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)
