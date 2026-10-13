"""Offline backend for RPCS3's own guest-memory dumps.

Utilities > Dump Guest Memory (recent RPCS3 builds) pauses the game and writes
a folder containing

    manifest.json     format "rpcs3-guest-memory-dump", region list
    guest_memory.bin  sparse 4 GiB image, file offset == guest address

That is exactly what a pointer scan or a snapshot diff needs, with no process
access and no IPC involved.  A plain raw file plus a start address is accepted
too (e.g. a region saved from Cheat Engine or the Memory Viewer).
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from .common import MemoryError_, Region, merge_regions, parse_int


class DumpError(MemoryError_):
    pass


class DumpMemory:
    name = "dump"
    writable = False

    def __init__(self, path: str, raw_base: Optional[int] = None):
        self.path = path
        self._info: dict = {}
        if os.path.isdir(path):
            manifest = os.path.join(path, "manifest.json")
            if not os.path.isfile(manifest):
                raise DumpError(f"{path}: no manifest.json (not an RPCS3 guest memory dump folder)")
            with open(manifest, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m.get("format") != "rpcs3-guest-memory-dump":
                raise DumpError(f"{manifest}: unexpected format {m.get('format')!r}")
            if not m.get("file_offset_equals_guest_address", True):
                raise DumpError("dump layout not supported: file offset != guest address")
            self.image = os.path.join(path, m.get("address_space_file", "guest_memory.bin"))
            self._regions = merge_regions(
                Region(parse_int(r["start"]), int(r["size"]),
                       bool(r.get("writable", True)), bool(r.get("executable", False)))
                for r in m.get("regions", []))
            self._info = {"title": m.get("title", ""), "id": m.get("title_id", ""),
                          "created_utc": m.get("created_utc", "")}
            self.offset = 0
        else:
            if raw_base is None:
                raise DumpError(f"{path} is a raw file: pass its guest start address (--raw-base)")
            self.image = path
            self.offset = raw_base
            self._regions = [Region(raw_base, os.path.getsize(path), True, False)]
        self.f = open(self.image, "rb")

    def read(self, addr: int, size: int) -> bytes:
        pos = addr - self.offset
        if pos < 0:
            raise DumpError(f"address {addr:#x} is below the dump start")
        self.f.seek(pos)
        data = self.f.read(size)
        if len(data) != size:
            raise DumpError(f"address {addr:#x}+{size:#x} is beyond the dump")
        return data

    def write(self, addr: int, data: bytes) -> None:
        raise DumpError("a dump is read-only")

    def regions(self) -> List[Region]:
        return list(self._regions)

    def game_info(self) -> dict:
        return dict(self._info)

    def close(self) -> None:
        self.f.close()
