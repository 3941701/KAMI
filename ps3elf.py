#!/usr/bin/env python3
"""ps3elf - static questions about a decrypted PS3 executable, answered without a debugger.

Official RPCS3 builds have no break-on-write, so "which code writes the camera
yaw?" has to be answered from the executable.  This tool does the mechanical
part; Ghidra is for reading the functions it points at.

    ps3elf.py info      EBOOT.elf
    ps3elf.py imports   EBOOT.elf [--grep cellPad]
    ps3elf.py callers   EBOOT.elf --import cellPadGetData [--depth 3]
    ps3elf.py fieldxref EBOOT.elf --offsets 0x1A8,0x1C4 [--ops stfs,lfs] [--min-match 2]
    ps3elf.py dataxref  EBOOT.elf --addr 0x00785494
    ps3elf.py floats    EBOOT.elf
    ps3elf.py patch     EBOOT.elf --hash PPU-... --bp 0x... [--nop 0x...] -o BCUS98119_patch.yml
    ...any of the four before it with  --ghidra findings.json  to save the hits for
       ghidra_scripts/KamiImportFindings.java

callers    who reads the pad: the functions calling an import, and their callers.
fieldxref  code that loads/stores a struct field at the given displacements -
           give it the yaw/pitch offsets inside the camera object (the last
           offsets of the pointer chain) to find the camera update code.
dataxref   code that refers to a fixed address, directly, through the TOC or via
           lis/addi (e.g. the FOV multiplier the RPCS3 wiki patch points at).
floats     functions that load angle constants (pi, 2*pi, pi/180 ...).
patch      an RPCS3 patch file that sets execution breakpoints at boot (bpex) and/or
           replaces instructions with nop.  `fieldxref --bp-patch FILE --hash PPU-...`
           writes one with a breakpoint on every store it found: boot the game with
           the PPU interpreter, turn the camera, and the debugger stops on the
           instruction that writes the angle.

Input must be a decrypted ELF: in RPCS3 use Utilities > Decrypt PS3 Binaries on
EBOOT.BIN (or  rpcs3 --decrypt <path>).  Addresses printed are the same guest
addresses RPCS3's debugger and KAMI use.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import struct
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

NID_SUFFIX = bytes.fromhex("6759659904250490566427499489741A")
PT_LOAD, PT_PROC_PRX_PARAM = 1, 0x60000002
PRX_PARAM_MAGIC = 0x1B434CEC

D_FORM = {32: "lwz", 33: "lwzu", 34: "lbz", 35: "lbzu", 36: "stw", 37: "stwu", 38: "stb", 39: "stbu",
          40: "lhz", 41: "lhzu", 42: "lha", 43: "lhau", 44: "sth", 45: "sthu",
          48: "lfs", 49: "lfsu", 50: "lfd", 51: "lfdu", 52: "stfs", 53: "stfsu", 54: "stfd", 55: "stfdu"}
FLOAT_OPS = {"lfs", "lfsu", "lfd", "lfdu", "stfs", "stfsu", "stfd", "stfdu"}
OP_BY_NAME = {v: k for k, v in D_FORM.items()}

ANGLE_CONSTANTS = {
    "pi": math.pi, "2pi": 2 * math.pi, "pi/2": math.pi / 2, "pi/4": math.pi / 4,
    "pi/180": math.pi / 180, "180/pi": 180 / math.pi, "1/2pi": 1 / (2 * math.pi), "1/pi": 1 / math.pi,
    "-pi": -math.pi, "-2pi": -2 * math.pi, "-pi/2": -math.pi / 2, "180": 180.0, "360": 360.0,
}


def fnid(name: str) -> int:
    """PS3 function NID: first four bytes of SHA-1(name + suffix), little-endian."""
    return struct.unpack("<I", hashlib.sha1(name.encode() + NID_SUFFIX).digest()[:4])[0]


def load_nid_names() -> Dict[int, str]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ps3_function_names.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = json.load(f)["names"]
    except (OSError, KeyError, ValueError):
        names = ["cellPadGetData", "cellPadGetDataExtra", "cellPadGetInfo2", "cellPadInit"]
    return {fnid(n): n for n in names}


def sx16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


class ElfError(Exception):
    pass


class Ps3Elf:
    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if d[:4] == b"SCE\0":
            raise ElfError("this is an encrypted SELF (EBOOT.BIN). Decrypt it first: RPCS3 > Utilities > "
                           "Decrypt PS3 Binaries, then run this tool on the resulting .elf")
        if d[:4] != b"\x7fELF" or d[4] != 2 or d[5] != 2:
            raise ElfError("not a 64-bit big-endian ELF")
        (self.e_type, self.e_machine) = struct.unpack_from(">HH", d, 16)
        self.e_entry, phoff = struct.unpack_from(">QQ", d, 24)
        phentsize, phnum = struct.unpack_from(">HH", d, 54)
        if self.e_machine != 21:
            raise ElfError(f"machine {self.e_machine} is not PowerPC64")
        self.segments: List[dict] = []
        for i in range(phnum):
            p_type, flags, off, vaddr, _pa, filesz, memsz, _al = struct.unpack_from(">IIQQQQQQ", d, phoff + i * phentsize)
            self.segments.append(dict(type=p_type, flags=flags, off=off, vaddr=vaddr, filesz=filesz, memsz=memsz))
        self.loads = [s for s in self.segments if s["type"] == PT_LOAD and s["filesz"]]
        if not self.loads:
            raise ElfError("no loadable segments")
        self.text = [s for s in self.loads if s["flags"] & 1]
        self.rw = [s for s in self.segments if s["type"] == PT_LOAD and s["flags"] & 2]
        # entry point is a function descriptor: (code address, TOC)
        try:
            self.entry_code, self.toc = struct.unpack(">II", self.read(self.e_entry, 8))
        except ElfError:
            self.entry_code, self.toc = 0, 0
        self._code: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self._funcs: Optional[List[int]] = None
        self._imports: Optional[List[dict]] = None
        self._bl: Optional[Tuple[np.ndarray, np.ndarray]] = None

    # -- raw access -------------------------------------------------------
    def offset(self, vaddr: int, size: int = 1) -> int:
        for s in self.loads:
            if s["vaddr"] <= vaddr and vaddr + size <= s["vaddr"] + s["filesz"]:
                return s["off"] + (vaddr - s["vaddr"])
        raise ElfError(f"address 0x{vaddr:08X} is not backed by the file")

    def read(self, vaddr: int, size: int) -> bytes:
        o = self.offset(vaddr, size)
        return self.data[o:o + size]

    def u32(self, vaddr: int) -> int:
        return struct.unpack(">I", self.read(vaddr, 4))[0]

    def has(self, vaddr: int, size: int = 4) -> bool:
        try:
            self.offset(vaddr, size)
            return True
        except ElfError:
            return False

    def cstr(self, vaddr: int, limit: int = 64) -> str:
        o = self.offset(vaddr)
        end = self.data.find(b"\0", o, o + limit)
        return self.data[o:end if end >= 0 else o + limit].decode("ascii", "replace")

    def in_text(self, vaddr: int) -> bool:
        return any(s["vaddr"] <= vaddr < s["vaddr"] + s["filesz"] for s in self.text)

    # -- code -------------------------------------------------------------
    def code(self) -> Tuple[np.ndarray, np.ndarray]:
        """(addresses, instruction words) of every word in the executable segments."""
        if self._code is None:
            addrs, words = [], []
            for s in self.text:
                n = s["filesz"] // 4
                w = np.frombuffer(self.data, dtype=">u4", count=n, offset=s["off"]).astype(np.uint32)
                addrs.append(s["vaddr"] + 4 * np.arange(n, dtype=np.int64))
                words.append(w)
            self._code = (np.concatenate(addrs), np.concatenate(words))
        return self._code

    def calls(self) -> Tuple[np.ndarray, np.ndarray]:
        """(site, target) of every direct `bl`."""
        if self._bl is None:
            addrs, words = self.code()
            m = (words & 0xFC000003) == 0x48000001
            li = (words[m] & 0x03FFFFFC).astype(np.int64)
            li = np.where(li & 0x02000000, li - 0x04000000, li)
            self._bl = (addrs[m], addrs[m] + li)
        return self._bl

    def functions(self) -> List[int]:
        """Function starts: every `bl` target plus every function descriptor found in data."""
        if self._funcs is None:
            starts = set()
            _sites, targets = self.calls()
            starts.update(int(t) for t in np.unique(targets) if self.in_text(int(t)))
            if self.toc:
                for s in self.rw:
                    if not s["filesz"]:
                        continue
                    n = s["filesz"] // 4
                    w = np.frombuffer(self.data, dtype=">u4", count=n, offset=s["off"])
                    # a descriptor is (code address, TOC); the TOC word identifies it
                    for i in np.flatnonzero(w[1:] == self.toc):
                        code_addr = int(w[i])
                        if code_addr % 4 == 0 and self.in_text(code_addr):
                            starts.add(code_addr)
            if self.entry_code:
                starts.add(self.entry_code)
            # A frame-building instruction right after a return starts a function nobody calls
            # directly (callbacks, virtual methods without a descriptor we recognised).
            addrs, words = self.code()
            after_ret = np.flatnonzero(words[:-1] == 0x4E800020) + 1
            w = words[after_ret]
            prologue = ((w & 0xFFFF0003) == 0xF8210001) | (w == 0x7C0802A6)     # stdu r1,-X(r1) / mflr r0
            starts.update(int(x) for x in addrs[after_ret[prologue]])
            self._funcs = sorted(starts)
        return self._funcs

    def func_of(self, addr: int) -> int:
        f = self.functions()
        i = bisect.bisect_right(f, addr) - 1
        return f[i] if i >= 0 else 0

    def descriptors_of(self, func: int) -> List[int]:
        """Addresses of function descriptors (OPD entries) for a code address."""
        out = []
        for s in self.rw:
            if not s["filesz"]:
                continue
            n = s["filesz"] // 4
            w = np.frombuffer(self.data, dtype=">u4", count=n, offset=s["off"])
            for i in np.flatnonzero((w[:-1] == func) & (w[1:] == self.toc)):
                out.append(s["vaddr"] + 4 * int(i))
        return out

    def data_refs(self, value: int) -> List[int]:
        """Data words equal to ``value`` (pointers to it: vtable slots, TOC entries...)."""
        # Read-only data (vtables, constant pools) shares the first, executable segment with
        # the code on PS3, so every loaded segment is searched.
        out = []
        for s in self.loads:
            n = s["filesz"] // 4
            w = np.frombuffer(self.data, dtype=">u4", count=n, offset=s["off"])
            out.extend(s["vaddr"] + 4 * int(i) for i in np.flatnonzero(w == value))
        return out

    # -- imports ----------------------------------------------------------
    def imports(self) -> List[dict]:
        if self._imports is not None:
            return self._imports
        names = load_nid_names()
        out: List[dict] = []
        for seg in self.segments:
            if seg["type"] != PT_PROC_PRX_PARAM or not seg["filesz"]:
                continue
            raw = self.data[seg["off"]:seg["off"] + 32]
            _size, magic, _ver, _unk, _ent_s, _ent_e, stub_s, stub_e = struct.unpack_from(">8I", raw, 0)
            if magic != PRX_PARAM_MAGIC:
                continue
            addr = stub_s
            while addr < stub_e:
                size = self.read(addr, 1)[0] or 0x2C
                num_func = struct.unpack(">H", self.read(addr + 6, 2))[0]
                name_p, nids_p, addrs_p = struct.unpack(">III", self.read(addr + 16, 12))
                lib = self.cstr(name_p) if name_p and self.has(name_p, 1) else "?"
                for i in range(num_func):
                    nid = self.u32(nids_p + 4 * i)
                    stub = self.u32(addrs_p + 4 * i)
                    out.append({"lib": lib, "nid": nid, "name": names.get(nid, f"nid_{nid:08X}"),
                                "stub": stub, "slot": addrs_p + 4 * i})
                addr += size
        self._imports = out
        return out

    # -- tiny disassembler for the report lines ---------------------------
    def dis(self, addr: int) -> str:
        try:
            w = self.u32(addr)
        except ElfError:
            return "??"
        op = w >> 26
        rt, ra, d = (w >> 21) & 31, (w >> 16) & 31, sx16(w & 0xFFFF)
        if op in D_FORM:
            reg = "f" if D_FORM[op] in FLOAT_OPS else "r"
            return f"{D_FORM[op]} {reg}{rt}, {'-' if d < 0 else ''}0x{abs(d):X}(r{ra})"
        if op == 14:
            return f"li r{rt}, {d}" if ra == 0 else f"addi r{rt}, r{ra}, {d}"
        if op == 15:
            return f"lis r{rt}, 0x{w & 0xFFFF:X}" if ra == 0 else f"addis r{rt}, r{ra}, 0x{w & 0xFFFF:X}"
        if op == 18:
            li = w & 0x03FFFFFC
            li = li - 0x04000000 if li & 0x02000000 else li
            tgt = li if w & 2 else addr + li
            return f"{'bl' if w & 1 else 'b'} 0x{tgt & 0xFFFFFFFF:08X}"
        if w == 0x4E800020:
            return "blr"
        if w == 0x4E800420:
            return "bctr"
        if w == 0x4E800421:
            return "bctrl"
        if w == 0x60000000:
            return "nop"
        return f".long 0x{w:08X}"


# --------------------------------------------------------------------------
class Findings:
    """Collects hits in the neutral format KamiImportFindings.java reads."""

    def __init__(self):
        self.items: List[dict] = []

    def add(self, addr: int, label: str = "", comment: str = "", category: str = "kami"):
        self.items.append({"addr": f"0x{addr:08X}", "label": label, "comment": comment, "category": category})

    def save(self, path: Optional[str]) -> None:
        if not path:
            return
        old = []
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f).get("items", [])
            except (OSError, ValueError):
                old = []
        seen = {(i["addr"], i["label"], i["comment"]) for i in old}
        merged = old + [i for i in self.items if (i["addr"], i["label"], i["comment"]) not in seen]
        # one finding per line: the Ghidra script reads the file line by line without a JSON library
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"format": "kami-findings", "version": 1, "items": [\n')
            f.write(",\n".join(json.dumps(i, ensure_ascii=True) for i in merged))
            f.write("\n]}\n")
        print(f"[{len(self.items)} находок добавлено в {path}]")


def write_rpcs3_patch(path: str, exe_hash: str, serial: str, title: str, name: str, notes: str,
                      lines: List[str]) -> None:
    """A per-title RPCS3 patch file (patches/<SERIAL>_patch.yml), patch engine version 1.2."""
    if not exe_hash.startswith("PPU-"):
        raise ElfError("--hash must be the executable hash as RPCS3 / KAMI show it, e.g. PPU-412a9b...")
    body = "\n".join(f"      - {line}" for line in lines)
    text = (f"Version: 1.2\n\n{exe_hash}:\n  \"{name}\":\n    Games:\n      \"{title}\":\n"
            f"        {serial}: [ All ]\n    Author: \"kami-toolkit\"\n    Notes: \"{notes}\"\n"
            f"    Patch Version: 1.0\n    Patch:\n{body}\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"[патч RPCS3 записан: {path}; положите его в папку patches как {serial}_patch.yml и "
          f"включите в Manage > Game Patches]")


def cmd_patch(elf: Ps3Elf, args, fnd: Findings) -> int:
    lines = []
    for a in args.bp or []:
        addr = int(a, 0)
        if not elf.in_text(addr) or addr % 4:
            raise ElfError(f"0x{addr:08X} is not an instruction address in this executable")
        lines.append(f"[ bpex, 0x{addr:08x}, 0 ]              # {elf.dis(addr)}")
    for a in args.nop or []:
        addr = int(a, 0)
        if not elf.in_text(addr) or addr % 4:
            raise ElfError(f"0x{addr:08X} is not an instruction address in this executable")
        lines.append(f"[ be32, 0x{addr:08x}, 0x60000000 ]     # was: {elf.dis(addr)}")
    if not lines:
        print("нечего записывать: задайте --bp и/или --nop")
        return 2
    notes = "Breakpoints need PPU Decoder = Interpreter (static)." if args.bp else "Removes the listed instructions."
    write_rpcs3_patch(args.output, args.hash, args.serial, args.title, args.name, notes, lines)
    return 0


def cmd_info(elf: Ps3Elf, args, fnd: Findings) -> int:
    print(f"entry descriptor 0x{elf.e_entry:08X} -> code 0x{elf.entry_code:08X}, TOC 0x{elf.toc:08X}")
    for s in elf.segments:
        kind = {1: "LOAD", 7: "TLS", 0x60000001: "PROC_PARAM", 0x60000002: "PRX_PARAM"}.get(s["type"], hex(s["type"]))
        fl = "".join(c if s["flags"] & b else "-" for c, b in (("r", 4), ("w", 2), ("x", 1)))
        print(f"  {kind:10s} {fl}  vaddr 0x{s['vaddr']:08X}  filesz 0x{s['filesz']:X}  memsz 0x{s['memsz']:X}")
    imps = elf.imports()
    libs = defaultdict(int)
    for i in imps:
        libs[i["lib"]] += 1
    print(f"imports: {len(imps)} functions from {len(libs)} libraries; functions found: {len(elf.functions())}")
    print("  " + ", ".join(f"{k}({v})" for k, v in sorted(libs.items())))
    return 0


def cmd_imports(elf: Ps3Elf, args, fnd: Findings) -> int:
    for i in elf.imports():
        if args.grep and args.grep.lower() not in (i["name"] + i["lib"]).lower():
            continue
        print(f"0x{i['stub']:08X}  {i['lib']:18s} {i['name']}  (nid 0x{i['nid']:08X})")
        fnd.add(i["stub"], i["name"], f"import stub: {i['lib']}::{i['name']}", "import")
    return 0


def callers_of(elf: Ps3Elf, target: int) -> List[int]:
    sites, targets = elf.calls()
    return [int(s) for s in sites[targets == target]]


def cmd_callers(elf: Ps3Elf, args, fnd: Findings) -> int:
    wanted = [w.strip() for w in args.imp.split(",")]
    imps = [i for i in elf.imports() if i["name"] in wanted]
    if not imps:
        print("импорт не найден; что есть: ps3elf.py imports FILE --grep <текст>")
        return 2
    for imp in imps:
        print(f"\n{imp['lib']}::{imp['name']}  stub 0x{imp['stub']:08X}")
        level = {imp["stub"]: [imp["name"]]}
        seen = set()
        for depth in range(1, args.depth + 1):
            nxt: Dict[int, List[str]] = {}
            for target, trail in level.items():
                for site in callers_of(elf, target):
                    func = elf.func_of(site)
                    if (func, target) in seen:
                        continue
                    seen.add((func, target))
                    print(f"{'  ' * depth}fn 0x{func:08X}  (call at 0x{site:08X})")
                    label = f"kami_pad_reader_L{depth}_{func:08X}"
                    fnd.add(func, label, f"reaches {imp['name']} at depth {depth} (call at 0x{site:08X})", "pad")
                    nxt.setdefault(func, trail + [f"0x{func:08X}"])
                    if depth == 1:
                        for desc in elf.descriptors_of(func):
                            refs = elf.data_refs(desc)
                            if refs:
                                print(f"{'  ' * depth}   descriptor 0x{desc:08X} referenced from data: " +
                                      ", ".join(f"0x{r:08X}" for r in refs[:6]) + (" ..." if len(refs) > 6 else ""))
            if not nxt:
                break
            level = nxt
    print("\n(только прямые вызовы bl; вызовы через указатели на функции видны как ссылки на дескриптор)")
    return 0


def cmd_fieldxref(elf: Ps3Elf, args, fnd: Findings) -> int:
    offsets = [int(o, 0) for o in args.offsets.split(",")]
    ops = [o.strip() for o in args.ops.split(",")]
    bad = [o for o in ops if o not in OP_BY_NAME]
    if bad:
        print(f"неизвестные инструкции: {bad}; доступны: {', '.join(sorted(OP_BY_NAME))}")
        return 2
    addrs, words = elf.code()
    opc = words >> 26
    ra = (words >> 16) & 31
    disp = (words & 0xFFFF).astype(np.int64)
    disp = np.where(disp & 0x8000, disp - 0x10000, disp)
    sel = np.isin(opc, [OP_BY_NAME[o] for o in ops]) & np.isin(disp, offsets)
    # r1 = stack frame, r2 = TOC, r13 = small data / TLS: not object fields
    sel &= ~np.isin(ra, [1, 2, 13])
    hits = defaultdict(list)
    for a, d in zip(addrs[sel].tolist(), disp[sel].tolist()):
        hits[elf.func_of(a)].append((a, d))
    ranked = sorted(hits.items(), key=lambda kv: (-len({d for _a, d in kv[1]}), -len(kv[1]), kv[0]))
    shown = 0
    for func, lst in ranked:
        distinct = sorted({d for _a, d in lst})
        if len(distinct) < args.min_match:
            continue
        stores = sum(1 for a, _d in lst if elf.dis(a).startswith("st"))
        print(f"\nfn 0x{func:08X}: смещений {len(distinct)}/{len(offsets)} "
              f"({', '.join(hex(d) for d in distinct)}), обращений {len(lst)}, из них записей {stores}")
        for a, d in lst[:args.lines]:
            print(f"    0x{a:08X}  {elf.dis(a)}")
            fnd.add(a, "", f"field +0x{d:X}: {elf.dis(a)}", "field")
        fnd.add(func, f"kami_field_user_{func:08X}",
                f"touches fields {', '.join(hex(d) for d in distinct)} ({stores} stores)", "field")
        for desc in elf.descriptors_of(func):
            refs = elf.data_refs(desc)
            if refs:
                # a descriptor stored in data is usually a vtable slot: objects of that class start
                # with a pointer to that table, which is a way to find them in memory
                print(f"    дескриптор 0x{desc:08X} лежит в данных по адресам: " +
                      ", ".join(f"0x{r:08X}" for r in refs[:6]) + (" ..." if len(refs) > 6 else "") +
                      "  (вероятно, слот таблицы виртуальных методов)")
        shown += 1
        if shown >= args.top:
            break
    if not shown:
        print("совпадений нет: проверьте смещения, попробуйте --min-match 1 или другой набор --ops")
        return 2
    if args.bp_patch:
        stores = []
        for func, lst in ranked:
            if len({d for _a, d in lst}) < args.min_match:
                continue
            stores += [a for a, _d in lst if elf.dis(a).startswith("st")]
        stores = sorted(set(stores))[:args.bp_limit]
        if not stores:
            print("в найденном нет инструкций записи - патч с брейкпоинтами не создан")
        else:
            write_rpcs3_patch(args.bp_patch, args.hash or "", args.serial, args.title,
                              "KAMI research: breakpoints on camera field stores",
                              "Breakpoints need PPU Decoder = Interpreter (static). They show up in the "
                              "debugger's breakpoint list when the game boots.",
                              [f"[ bpex, 0x{a:08x}, 0 ]              # {elf.dis(a)}" for a in stores])
    print(f"\nпоказано функций: {shown} из {len(ranked)}. Функции с записями (stfs) в оба смещения - "
          f"первые кандидаты на код обновления камеры.")
    return 0


def toc_slot_loads(elf: Ps3Elf, slot: int) -> List[int]:
    """Instructions `lwz rX, d(r2)` loading the TOC slot at address ``slot``."""
    d = slot - elf.toc
    if not (-0x8000 <= d <= 0x7FFF):
        return []
    addrs, words = elf.code()
    want = (32 << 26) | (2 << 16) | (d & 0xFFFF)
    return addrs[(words & 0xFC1FFFFF) == want].tolist()


def refs_to_address(elf: Ps3Elf, target: int) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    addrs, words = elf.code()
    opc = words >> 26
    ra = (words >> 16) & 31
    disp = (words & 0xFFFF).astype(np.int64)
    disp = np.where(disp & 0x8000, disp - 0x10000, disp)
    # 1. straight off the TOC register
    d = target - elf.toc
    if elf.toc and -0x8000 <= d <= 0x7FFF:
        m = (ra == 2) & (disp == d) & (np.isin(opc, list(D_FORM)) | (opc == 14))
        out += [(int(a), "TOC-relative") for a in addrs[m]]
    # 2. through a pointer kept in a data word (TOC entry) that is itself loaded off r2
    for slot in elf.data_refs(target):
        for a in toc_slot_loads(elf, slot):
            out.append((int(a), f"via pointer at 0x{slot:08X}"))
    # 3. lis/addis + low half within the next few instructions
    hi = ((target + 0x8000) >> 16) & 0xFFFF
    lo = sx16(target & 0xFFFF)
    lis = np.flatnonzero((opc == 15) & ((words & 0xFFFF) == hi))
    for i in lis.tolist():
        reg = int((words[i] >> 21) & 31)
        for j in range(i + 1, min(i + 9, len(words))):
            w = int(words[j])
            if (w >> 26) in D_FORM or (w >> 26) in (14, 24):
                if ((w >> 16) & 31) == reg and (sx16(w & 0xFFFF) == lo or ((w >> 26) == 24 and (w & 0xFFFF) == (target & 0xFFFF))):
                    out.append((int(addrs[j]), f"lis at 0x{int(addrs[i]):08X}"))
                    break
    return sorted(set(out))


def cmd_dataxref(elf: Ps3Elf, args, fnd: Findings) -> int:
    target = int(args.addr, 0)
    refs = refs_to_address(elf, target)
    if elf.has(target):
        raw = elf.read(target, 4)
        print(f"0x{target:08X}: {raw.hex()}  float {struct.unpack('>f', raw)[0]:.6g}  "
              f"{'(сегмент кода и констант)' if elf.in_text(target) else '(сегмент данных)'}")
    if not refs:
        print("ссылок не найдено (адрес может вычисляться или лежать в структуре, на которую ссылаются по базе)")
        return 2
    by_func = defaultdict(list)
    for a, how in refs:
        by_func[elf.func_of(a)].append((a, how))
    for func, lst in sorted(by_func.items()):
        print(f"\nfn 0x{func:08X}")
        fnd.add(func, f"kami_refs_{target:08X}_{func:08X}", f"references 0x{target:08X}", "data")
        for a, how in lst:
            print(f"    0x{a:08X}  {elf.dis(a):28s} {how}")
            fnd.add(a, "", f"reference to 0x{target:08X} ({how})", "data")
    return 0


def cmd_floats(elf: Ps3Elf, args, fnd: Findings) -> int:
    consts = {}
    for s in elf.loads:            # constant pools live in .rodata, inside the executable segment
        n = s["filesz"] // 4
        f = np.frombuffer(elf.data, dtype=">f4", count=n, offset=s["off"])
        for name, value in ANGLE_CONSTANTS.items():
            with np.errstate(invalid="ignore"):
                idx = np.flatnonzero(np.abs(f - np.float32(value)) <= abs(value) * 2e-7)
            for i in idx.tolist():
                consts[s["vaddr"] + 4 * i] = name
    per_func = defaultdict(lambda: defaultdict(list))
    for addr, name in consts.items():
        for a, _how in refs_to_address(elf, addr):
            per_func[elf.func_of(a)][name].append(a)
    ranked = sorted(per_func.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    print(f"констант углов в данных: {len(consts)}; функций, которые их читают: {len(ranked)}")
    for func, names in ranked[:args.top]:
        if len(names) < args.min_match:
            break
        print(f"fn 0x{func:08X}: " + ", ".join(f"{n} x{len(v)}" for n, v in sorted(names.items())))
        fnd.add(func, f"kami_angle_math_{func:08X}", "loads angle constants: " + ", ".join(sorted(names)), "angle")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("elf")
        p.add_argument("--ghidra", metavar="FILE", help="append the hits to a findings file for Ghidra")

    p = sub.add_parser("info"); common(p); p.set_defaults(func=cmd_info)
    p = sub.add_parser("imports"); common(p); p.add_argument("--grep"); p.set_defaults(func=cmd_imports)
    p = sub.add_parser("callers"); common(p)
    p.add_argument("--import", dest="imp", default="cellPadGetData,cellPadGetDataExtra")
    p.add_argument("--depth", type=int, default=3)
    p.set_defaults(func=cmd_callers)
    p = sub.add_parser("fieldxref"); common(p)
    p.add_argument("--offsets", required=True, help="comma separated displacements, e.g. 0x1A8,0x1C4")
    p.add_argument("--ops", default="lfs,stfs,lfsu,stfsu")
    p.add_argument("--min-match", type=int, default=1, help="distinct offsets a function must touch")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--lines", type=int, default=12)
    p.add_argument("--bp-patch", metavar="FILE",
                   help="also write an RPCS3 patch that sets an execution breakpoint on every store found")
    p.add_argument("--bp-limit", type=int, default=64)
    p.add_argument("--hash", help="executable hash (PPU-...), needed for --bp-patch")
    p.add_argument("--serial", default="BCUS98119")
    p.add_argument("--title", default="inFAMOUS")
    p.set_defaults(func=cmd_fieldxref)
    p = sub.add_parser("patch", help="write an RPCS3 patch file with breakpoints and/or NOPs"); common(p)
    p.add_argument("--bp", action="append", metavar="ADDR", help="execution breakpoint (repeatable)")
    p.add_argument("--nop", action="append", metavar="ADDR", help="replace the instruction with nop (repeatable)")
    p.add_argument("--hash", required=True, help="executable hash (PPU-...)")
    p.add_argument("--serial", default="BCUS98119")
    p.add_argument("--title", default="inFAMOUS")
    p.add_argument("--name", default="KAMI research patch")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_patch)
    p = sub.add_parser("dataxref"); common(p); p.add_argument("--addr", required=True); p.set_defaults(func=cmd_dataxref)
    p = sub.add_parser("floats"); common(p)
    p.add_argument("--min-match", type=int, default=2)
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(func=cmd_floats)

    args = ap.parse_args(argv)
    try:
        elf = Ps3Elf(args.elf)
        fnd = Findings()
        rc = args.func(elf, args, fnd)
        fnd.save(args.ghidra)
        return rc
    except ElfError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
