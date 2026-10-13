#!/usr/bin/env python3
"""The RPCS3 cheat "redirection script" produced by ptrscan must resolve to the same address as a
KAMI DerefChain.  resolve() below is a line-for-line port of cheat_engine::resolve_script."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ptrscan import rpcs3_cheat_script                 # noqa: E402
from rpcs3mem.backend import resolve_chain             # noqa: E402

MEM = {0x00A12340: 0x30250370, 0x30250370 + 0x5C: 0x30250800, 0x30250800 + 0x118: 0x30251720}


class Mem:
    def read(self, addr, size):
        if addr not in MEM:
            from rpcs3mem.common import MemoryError_
            raise MemoryError_("unmapped")
        return struct.pack(">I", MEM[addr])


def resolve(offset, script):
    final, op, i = 0, "=", 0

    def apply(cur, value):
        return value if op == "=" else (cur + value) & 0xFFFFFFFF if op == "+" else (cur - value) & 0xFFFFFFFF

    while i < len(script):
        c = script[i]
        if c.isdigit():
            j = i
            while j < len(script) and script[j].isdigit():
                j += 1
            final = apply(final, int(script[i:j]))
            i = j
        elif c == "$":
            final = apply(final, offset)
            i += 1
        elif c == "[":
            depth, sub = 1, ""
            i += 1
            while i < len(script):
                if depth == 0:
                    break
                if script[i] == "]":
                    depth -= 1
                elif script[i] == "[":
                    depth += 1
                if depth != 0:
                    sub += script[i]
                i += 1
            assert depth == 0
            inner = resolve(offset, sub)
            if inner is None or inner not in MEM:
                return None
            final = apply(final, MEM[inner])
        elif c in "+-":
            op = c
            i += 1
        elif c == " ":
            i += 1
        else:
            raise ValueError(c)
    return final


offsets = [0x5C, 0x118, 0x1A8]
script = rpcs3_cheat_script([0x00A12340] + offsets)
want = resolve_chain(Mem(), 0x00A12340, offsets)
got = resolve(7, script)                               # the cheat's own offset is irrelevant
print(f"script {script} -> 0x{got:08X}, DerefChain -> 0x{want:08X}")
assert got == want == 0x30251720 + 0x1A8
assert resolve(1, rpcs3_cheat_script([0x00A12340, 0x10])) == 0x30250380
assert resolve(1, rpcs3_cheat_script([0x00A12348, 0x10])) is None          # unmapped hop
print("CHEAT SCRIPT OK")
