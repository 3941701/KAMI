#!/usr/bin/env python3
"""Builds a small PS3-style executable for testing ps3elf.py and the Ghidra scripts.

The file follows the PS3 conventions the tools rely on (taken from RPCS3's
loader): 64-bit big-endian PowerPC ELF loaded at 0x10000, 8-byte function
descriptors (code address, TOC), a PT 0x60000002 segment pointing at the
import stub table, the standard 8-instruction import stub, constants reached
through TOC entries.  The code is a miniature of the thing being hunted: a pad
poll that calls cellPadGetData and a camera update that adds to two floats at
+0x1A8 / +0x1C4 of an object.

With --check-encodings every instruction is also assembled by llvm-mc and
compared, so the encoders below are known to be right.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys

TEXT, DATA = 0x10000, 0x30000
TOC_BASE = DATA + 0x100 + 0x8000


class Asm:
    def __init__(self, base: int):
        self.base, self.words, self.src, self.labels, self.fix = base, [], [], {}, []

    @property
    def pc(self) -> int:
        return self.base + 4 * len(self.words)

    def label(self, name: str) -> None:
        self.labels[name] = self.pc

    def emit(self, word: int, text: str) -> None:
        self.words.append(word & 0xFFFFFFFF)
        self.src.append(text)

    def d(self, op, name, rt, ra, disp, f=False):
        self.emit((op << 26) | (rt << 21) | (ra << 16) | (disp & 0xFFFF), f"{name} {rt}, {disp}({ra})")

    def lwz(self, rt, ra, disp): self.d(32, "lwz", rt, ra, disp)
    def stw(self, rt, ra, disp): self.d(36, "stw", rt, ra, disp)
    def lfs(self, rt, ra, disp): self.d(48, "lfs", rt, ra, disp)
    def stfs(self, rt, ra, disp): self.d(52, "stfs", rt, ra, disp)
    def addi(self, rt, ra, v): self.emit((14 << 26) | (rt << 21) | (ra << 16) | (v & 0xFFFF), f"addi {rt}, {ra}, {v}")
    def li(self, rt, v): self.emit((14 << 26) | (rt << 21) | (v & 0xFFFF), f"li {rt}, {v}")
    def lis(self, rt, v): self.emit((15 << 26) | (rt << 21) | (v & 0xFFFF), f"lis {rt}, {v}")
    def oris(self, ra, rs, v): self.emit((25 << 26) | (rs << 21) | (ra << 16) | (v & 0xFFFF), f"oris {ra}, {rs}, {v}")
    def std(self, rs, ra, disp): self.emit((62 << 26) | (rs << 21) | (ra << 16) | (disp & 0xFFFC), f"std {rs}, {disp}({ra})")
    def stdu(self, rs, ra, disp): self.emit((62 << 26) | (rs << 21) | (ra << 16) | (disp & 0xFFFC) | 1, f"stdu {rs}, {disp}({ra})")
    def ld(self, rt, ra, disp): self.emit((58 << 26) | (rt << 21) | (ra << 16) | (disp & 0xFFFC), f"ld {rt}, {disp}({ra})")
    def fadds(self, t, a, b): self.emit((59 << 26) | (t << 21) | (a << 16) | (b << 11) | (21 << 1), f"fadds {t}, {a}, {b}")
    def mr(self, ra, rs): self.emit((31 << 26) | (rs << 21) | (ra << 16) | (rs << 11) | (444 << 1), f"mr {ra}, {rs}")
    def mflr(self, r): self.emit(0x7C0802A6 | (r << 21), f"mflr {r}")
    def mtlr(self, r): self.emit(0x7C0803A6 | (r << 21), f"mtlr {r}")
    def mtctr(self, r): self.emit(0x7C0903A6 | (r << 21), f"mtctr {r}")
    def blr(self): self.emit(0x4E800020, "blr")
    def bctr(self): self.emit(0x4E800420, "bctr")
    def nop(self): self.emit(0x60000000, "nop")

    def bl(self, target: str):
        self.fix.append((len(self.words), target))
        self.emit(0x48000001, f"bl {target}")

    def finish(self) -> bytes:
        for idx, target in self.fix:
            off = self.labels[target] - (self.base + 4 * idx)
            self.words[idx] = 0x48000001 | (off & 0x03FFFFFC)
        return b"".join(struct.pack(">I", w) for w in self.words)

    def prologue(self, frame=0x70):
        self.stdu(1, 1, -frame)
        self.mflr(0)
        self.std(0, 1, frame + 0x10)

    def epilogue(self, frame=0x70):
        self.ld(0, 1, frame + 0x10)
        self.addi(1, 1, frame)
        self.mtlr(0)
        self.blr()


def build() -> tuple:
    a = Asm(TEXT + 0x200)
    # TOC entries (data + 0x100): pointers to constants in rodata
    toc = {"pi": DATA + 0x100, "twopi": DATA + 0x104, "deg2rad": DATA + 0x108}
    ro = {"pi": TEXT + 0x1000, "twopi": TEXT + 0x1004, "deg2rad": TEXT + 0x1008, "fov": TEXT + 0x100C,
          "libname": TEXT + 0x1010, "fnid": TEXT + 0x1020}
    slot = DATA + 0x200                      # import address table entry (patched by the loader)
    libstub = TEXT + 0x1040

    a.label("camera_update")                 # r3 = camera, f1 = d_yaw, f2 = d_pitch
    a.prologue()
    a.lfs(0, 3, 0x1A8)
    a.fadds(0, 0, 1)
    a.lwz(9, 2, toc["pi"] - TOC_BASE)
    a.lfs(13, 9, 0)
    a.lwz(9, 2, toc["twopi"] - TOC_BASE)
    a.lfs(12, 9, 0)
    a.stfs(0, 3, 0x1A8)
    a.lfs(0, 3, 0x1C4)
    a.fadds(0, 0, 2)
    a.stfs(0, 3, 0x1C4)
    a.epilogue()

    a.label("camera_read")                   # only reads the two fields
    a.lfs(1, 3, 0x1A8)
    a.lfs(2, 3, 0x1C4)
    a.blr()

    a.label("unrelated")                     # touches 0x1A8 of something else, once
    a.lfs(1, 4, 0x1A8)
    a.lwz(9, 2, toc["deg2rad"] - TOC_BASE)
    a.lfs(2, 9, 0)
    a.blr()

    a.label("fov_user")                      # absolute address by lis + low half
    hi = ((ro["fov"] + 0x8000) >> 16) & 0xFFFF
    a.lis(9, hi)
    a.lfs(1, 9, ro["fov"] - (hi << 16))
    a.blr()

    a.label("input_poll")                    # r3 = game
    a.prologue(0xC0)
    a.mr(31, 3)
    a.li(3, 0)
    a.addi(4, 1, 0x70)
    a.bl("cellPadGetData_stub")
    a.ld(2, 1, 0x28)
    a.lwz(3, 31, 0x5C)
    a.lwz(3, 3, 0x118)
    a.bl("camera_update")
    a.nop()
    a.epilogue(0xC0)

    a.label("game_frame")
    a.prologue()
    a.bl("input_poll")
    a.nop()
    a.bl("camera_read")
    a.nop()
    a.epilogue()

    a.label("start")
    a.prologue()
    a.bl("game_frame")
    a.nop()
    a.bl("fov_user")
    a.nop()
    a.epilogue()

    a.label("cellPadGetData_stub")           # the standard PS3 import stub
    a.li(12, 0)
    a.oris(12, 12, slot >> 16)
    a.lwz(12, 12, slot & 0xFFFF)
    a.std(2, 1, 0x28)
    a.lwz(0, 12, 0)
    a.lwz(2, 12, 4)
    a.mtctr(0)
    a.bctr()
    code = a.finish()
    L = a.labels

    text = bytearray(0x1100)
    text[0x200:0x200 + len(code)] = code
    struct.pack_into(">4f", text, 0x1000, 3.14159265, 6.2831853, 0.017453292, 1.0)
    text[0x1010:0x1017] = b"sys_io\0"
    struct.pack_into(">I", text, 0x1020, 0x8B72CDA1)                         # FNID of cellPadGetData
    # import library entry (RPCS3: ppu_prx_module_info), 0x2C bytes
    struct.pack_into(">BBHHHHHBB2xIIIIIII", text, 0x1040, 0x2C, 0, 1, 9, 1, 0, 0, 0, 0,
                     ro["libname"], ro["fnid"], slot, 0, 0, 0, 0)

    data = bytearray(0x400)
    opd = {"start": DATA + 0x00, "camera_update": DATA + 0x08, "input_poll": DATA + 0x10}
    for name, at in opd.items():
        struct.pack_into(">II", data, at - DATA, L[name], TOC_BASE)
    struct.pack_into(">III", data, 0x100, ro["pi"], ro["twopi"], ro["deg2rad"])   # TOC
    struct.pack_into(">I", data, 0x200, L["cellPadGetData_stub"])                 # stub address table
    struct.pack_into(">I", data, 0x240, opd["camera_update"])                     # a "vtable" slot
    # sys_proc_prx_param: size, magic, version, unk, libent start/end, libstub start/end
    struct.pack_into(">8I", data, 0x300, 0x40, 0x1B434CEC, 4, 0, 0, 0, libstub, libstub + 0x2C)

    ehdr = struct.pack(">4sBBBBB7xHHIQQQIHHHHHH", b"\x7fELF", 2, 2, 1, 0x66, 0, 2, 21, 1,
                       opd["start"], 64, 0, 0, 64, 56, 3, 64, 0, 0)
    ph = struct.pack(">IIQQQQQQ", 1, 5, 0, TEXT, TEXT, len(text), len(text), 0x10000)
    ph += struct.pack(">IIQQQQQQ", 1, 6, 0x2000, DATA, DATA, len(data), len(data) + 0x1000, 0x10000)
    ph += struct.pack(">IIQQQQQQ", 0x60000002, 4, 0x2000 + 0x300, DATA + 0x300, DATA + 0x300, 0x40, 0x40, 4)
    text[0:len(ehdr) + len(ph)] = ehdr + ph
    image = bytes(text) + b"\0" * (0x2000 - len(text)) + bytes(data)
    truth = {"labels": {k: f"0x{v:08X}" for k, v in L.items()}, "toc": f"0x{TOC_BASE:08X}",
             "fov": f"0x{ro['fov']:08X}", "opd": {k: f"0x{v:08X}" for k, v in opd.items()},
             "vtable_slot": f"0x{DATA + 0x240:08X}"}
    return image, truth, a


def check_encodings(a: Asm) -> int:
    """Assemble every instruction with llvm-mc and compare with our own encoding."""
    lines = []
    fix = dict(a.fix)
    for i, text in enumerate(a.src):
        # a branch is checked with its resolved displacement written as a number
        lines.append(f"bl (0+{a.labels[fix[i]] - (a.base + 4 * i)})" if text.startswith("bl ") else text)
    out = subprocess.run(["llvm-mc", "-triple=powerpc64-unknown-elf", "-show-encoding"],
                         input="\n".join(lines) + "\n", capture_output=True, text=True, check=True).stdout
    enc = []
    for line in out.splitlines():
        if "encoding: [" in line:
            hx = line.split("encoding: [", 1)[1].split("]")[0].replace("0x", "").replace(",", "")
            enc.append(hx)
    bad = 0
    for i, (text, word) in enumerate(zip(a.src, a.words)):
        ours = f"{word:08x}"
        ref = enc[i].lower()
        if ours != ref:
            print(f"ENCODING MISMATCH {text!r}: ours {ours} llvm-mc {ref}")
            bad += 1
    print(f"encodings checked against llvm-mc: {len(a.src)} instructions, {bad} mismatches")
    return bad


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "test_game.elf"
    image, truth, asm = build()
    with open(out, "wb") as f:
        f.write(image)
    with open(out + ".truth.json", "w") as f:
        json.dump(truth, f, indent=1)
    print(json.dumps(truth, indent=1))
    if "--check-encodings" in sys.argv:
        sys.exit(1 if check_encodings(asm) else 0)
