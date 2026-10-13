"""Snapshot and bulk-read helpers built on numpy.

Guest memory is treated as an array of 4-byte big-endian words on 4-byte
alignment, which is how PS3 games lay out floats and pointers.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np

from .common import VIDEO_START, MemoryError_, Region, clip_regions

CHUNK = 8 << 20
NAN_BITS = 0x7FC00000


def scan_regions(mem, lo: int = 0x10000, hi: int = VIDEO_START,
                 min_size: int = 0) -> List[Region]:
    """Readable guest regions inside [lo, hi), word-aligned.

    The default range skips RSX local memory (0xC0000000), thread stacks
    (0xD0000000) and SPU memory: the first two are mapped whole by RPCS3 and
    hold no persistent game state worth scanning.
    """
    out = []
    for r in clip_regions(mem.regions(), lo, hi):
        start = (r.start + 3) & ~3
        size = (r.end - start) & ~3
        if size > 0 and size >= min_size:
            out.append(Region(start, size, r.writable, r.executable))
    return out


def read_region(mem, region: Region, chunk: int = CHUNK) -> np.ndarray:
    """Whole region as uint8; unreadable pieces come back as zeros."""
    buf = np.zeros(region.size, dtype=np.uint8)
    off = 0
    while off < region.size:
        n = min(chunk, region.size - off)
        try:
            buf[off:off + n] = np.frombuffer(mem.read(region.start + off, n), dtype=np.uint8)
        except MemoryError_:
            # retry page by page so one vanished page does not cost the chunk
            for p in range(off, off + n, 0x1000):
                m = min(0x1000, off + n - p)
                try:
                    buf[p:p + m] = np.frombuffer(mem.read(region.start + p, m), dtype=np.uint8)
                except MemoryError_:
                    pass
        off += n
    return buf


class Snapshot:
    """Raw copy of a set of regions."""

    def __init__(self, mem, regions: Sequence[Region], progress=None):
        self.regions = list(regions)
        self.data: List[np.ndarray] = []
        done = 0
        total = sum(r.size for r in self.regions)
        for r in self.regions:
            self.data.append(read_region(mem, r))
            done += r.size
            if progress:
                progress(done, total)

    @property
    def nbytes(self) -> int:
        return sum(d.nbytes for d in self.data)

    def words(self, i: int) -> np.ndarray:
        return self.data[i].view(">u4")


def changed_mask(a: Snapshot, b: Snapshot) -> List[np.ndarray]:
    """Per-region boolean arrays: word differs between the two snapshots."""
    return [a.words(i) != b.words(i) for i in range(len(a.regions))]


def mask_to_addrs(regions: Sequence[Region], masks: Sequence[np.ndarray]) -> np.ndarray:
    parts = [r.start + 4 * np.flatnonzero(m).astype(np.uint64) for r, m in zip(regions, masks)]
    return np.concatenate(parts).astype(np.uint32) if parts else np.zeros(0, dtype=np.uint32)


def words_at(snap: Snapshot, addrs: np.ndarray) -> np.ndarray:
    """Raw big-endian words of a snapshot at the given (sorted or unsorted) addresses."""
    out = np.full(len(addrs), NAN_BITS, dtype=">u4")
    a64 = addrs.astype(np.int64)
    for i, r in enumerate(snap.regions):
        sel = (a64 >= r.start) & (a64 < r.end)
        if sel.any():
            out[sel] = snap.words(i)[(a64[sel] - r.start) // 4]
    return out


def read_words(mem, addrs: np.ndarray, gap: int = 0x2000) -> np.ndarray:
    """Current raw big-endian words at ``addrs``; unreadable ones become a NaN pattern.

    Addresses are grouped into spans (holes smaller than ``gap`` are read
    through) so a few thousand scattered candidates cost a handful of reads.
    """
    n = len(addrs)
    out = np.full(n, NAN_BITS, dtype=">u4")
    if n == 0:
        return out
    fast = getattr(mem, "read_u32_many", None)
    if fast is not None:
        return fast(addrs)
    order = np.argsort(addrs, kind="stable")
    sa = addrs[order].astype(np.int64)
    breaks = np.flatnonzero(np.diff(sa) > gap) + 1
    starts = np.concatenate(([0], breaks))
    ends = np.concatenate((breaks, [n]))
    for s, e in zip(starts, ends):
        lo, hi = int(sa[s]), int(sa[e - 1]) + 4
        try:
            raw = np.frombuffer(mem.read(lo, hi - lo), dtype=np.uint8)
        except MemoryError_:
            if e - s == 1:
                continue
            # split the span: one bad page should not blank its neighbours
            mid = (s + e) // 2
            for s2, e2 in ((s, mid), (mid, e)):
                out[order[s2:e2]] = read_words(mem, addrs[order[s2:e2]], gap)
            continue
        idx = sa[s:e] - lo
        vals = (raw[idx].astype(np.uint32) << 24) | (raw[idx + 1].astype(np.uint32) << 16) | \
               (raw[idx + 2].astype(np.uint32) << 8) | raw[idx + 3].astype(np.uint32)
        out[order[s:e]] = vals
    return out


def as_float(words: np.ndarray) -> np.ndarray:
    """Reinterpret raw big-endian words as float32 (native-endian result)."""
    return np.ascontiguousarray(words, dtype=">u4").view(">f4").astype(np.float32)


def plausible_float(f: np.ndarray, limit: float = 1.0e5) -> np.ndarray:
    """Finite and not absurdly large.

    There is deliberately no lower bound: an angle that happens to sit at
    1e-11 is still an angle.  Integers and pointers read as floats are either
    huge or denormal, and denormals never move by more than the 'changed'
    threshold, so the movement tests remove them anyway.
    """
    with np.errstate(invalid="ignore"):
        return np.isfinite(f) & (np.abs(f) <= limit)


def parse_elf_segments(mem) -> Optional[List[Tuple[int, int, int]]]:
    """PT_LOAD segments (vaddr, memsz, flags) of the main executable, read from guest memory.

    PS3 executables are loaded with their ELF header at 0x10000, so the
    program headers are normally readable straight from the running game.
    """
    import struct
    try:
        eh = mem.read(0x10000, 64)
    except MemoryError_:
        return None
    if eh[:4] != b"\x7fELF" or eh[4] != 2 or eh[5] != 2:
        return None
    phoff = struct.unpack_from(">Q", eh, 32)[0]
    phentsize, phnum = struct.unpack_from(">HH", eh, 54)
    if phentsize != 56 or not (0 < phnum <= 32) or phoff > 0x1000:
        return None
    try:
        ph = mem.read(0x10000 + phoff, phentsize * phnum)
    except MemoryError_:
        return None
    segs = []
    for i in range(phnum):
        p_type, p_flags, _off, vaddr, _paddr, _filesz, memsz, _align = \
            struct.unpack_from(">IIQQQQQQ", ph, i * 56)
        if p_type == 1 and memsz and vaddr < 0x1_0000_0000:
            segs.append((int(vaddr), int(memsz), int(p_flags)))
    return segs or None


def static_ranges(mem) -> List[Tuple[int, int]]:
    """Address ranges holding the executable's own writable data (.data/.bss/.toc).

    These are the only places a pointer path may start if it is to survive a
    restart.  Falls back to 'everything below 0x20000000 that is mapped' when
    the ELF header cannot be read.
    """
    segs = parse_elf_segments(mem)
    if segs:
        rw = [(v, v + n) for v, n, fl in segs if fl & 2]
        if rw:
            return rw
    return [(r.start, r.end) for r in mem.regions() if r.start < 0x20000000]
