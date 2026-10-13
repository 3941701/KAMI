#!/usr/bin/env python3
"""gdbtrace - log which candidate instructions execute, and on what address, via RPCS3's GDB stub.

EXPERIMENTAL.  Written from the RPCS3 source (rpcs3/Emu/GDB.cpp) and tested
only against a stand-in that mimics that code, not against a live RPCS3.

The static search (ps3elf.py fieldxref) usually returns several instructions
that could be writing the camera angle.  This tool finds out which of them
really run while you turn the camera, and what they access:

    gdbtrace.py --findings findings.json --category field --watch 0x30251BA8
    gdbtrace.py --addr 0x00123450 --addr 0x00123478 --seconds 20

It plants an execution breakpoint on each candidate, lets the game run, and on
every hit records the thread, LR (the caller) and - for a load/store - the
effective address  GPR[rA] + displacement.  A hit whose effective address
equals a --watch address is the instruction you are looking for.

Requirements and side effects, all consequences of how the stub is written:
  * PPU Decoder must be "Interpreter (static)": with the LLVM recompiler the
    breakpoints are silently ignored (the stub still answers OK).
  * The stub listens on 127.0.0.1:2345 while a game runs (config.yml,
    Miscellaneous > "GDB Server").  Connecting pauses emulation.
  * It cannot be left politely: when this tool disconnects, RPCS3 logs a fatal
    error for its "GDB Server" thread and stays paused.  Press Resume; the stub
    is gone until the game is booted again.  Breakpoints are removed first.
The same can be done by hand in View > Show Debugger; this just automates it.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from typing import Dict, List, Optional

D_FORM = {32: "lwz", 33: "lwzu", 34: "lbz", 36: "stw", 37: "stwu", 38: "stb", 40: "lhz", 44: "sth",
          48: "lfs", 49: "lfsu", 50: "lfd", 51: "lfdu", 52: "stfs", 53: "stfsu", 54: "stfd", 55: "stfdu"}
REG_PC, REG_LR = 64, 67


class GdbError(Exception):
    pass


class GdbClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 2345, timeout: float = 5.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise GdbError(f"cannot connect to the RPCS3 GDB stub at {host}:{port}: {e}. A game must be "
                           f"running, and 'GDB Server' in config.yml must not be empty.") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""

    # -- packets ----------------------------------------------------------
    def _getc(self, timeout: Optional[float]) -> Optional[bytes]:
        if not self.buf:
            self.sock.settimeout(timeout)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise GdbError("RPCS3 closed the GDB connection")
            self.buf = chunk
        c, self.buf = self.buf[:1], self.buf[1:]
        return c

    def send(self, data: str) -> None:
        raw = data.encode("ascii")
        packet = b"$" + raw + b"#" + b"%02x" % (sum(raw) & 0xFF)
        for _ in range(5):
            self.sock.sendall(packet)
            c = self._getc(5.0)
            if c == b"+":
                return
            if c is None:
                raise GdbError("no acknowledgement from the stub")
        raise GdbError("stub keeps rejecting the packet")

    def recv(self, timeout: Optional[float] = 5.0) -> Optional[str]:
        """One reply packet; None on timeout."""
        while True:
            c = self._getc(timeout)
            if c is None:
                return None
            if c == b"$":
                break
        data = bytearray()
        while True:
            c = self._getc(5.0)
            if c is None:
                raise GdbError("truncated packet")
            if c == b"#":
                break
            if c == b"}":
                c = bytes([self._getc(5.0)[0] ^ 0x20])
            data += c
        self._getc(5.0)
        self._getc(5.0)                      # checksum, not verified
        self.sock.sendall(b"+")
        return data.decode("ascii", "replace")

    def cmd(self, data: str, timeout: float = 5.0) -> str:
        self.send(data)
        reply = self.recv(timeout)
        if reply is None:
            raise GdbError(f"no reply to {data!r}")
        return reply

    # -- operations -------------------------------------------------------
    def set_bp(self, addr: int) -> None:
        if self.cmd(f"Z0,{addr:x},4") != "OK":
            raise GdbError(f"stub refused breakpoint at 0x{addr:08X}")

    def clear_bp(self, addr: int) -> None:
        self.cmd(f"z0,{addr:x},4")

    def current_thread(self) -> Optional[int]:
        r = self.cmd("qC")
        return int(r[2:], 16) if r.startswith("QC") and len(r) > 2 else None

    def select_thread(self, tid: int) -> None:
        # Without this the stub reads registers from the FIRST PPU thread, not the one that stopped.
        self.cmd(f"Hg{tid:x}")

    def registers(self) -> List[Optional[int]]:
        raw = self.cmd("g")
        regs: List[Optional[int]] = []
        pos = 0
        for i in range(71):
            width = 8 if i in (66, 69, 70) else 16
            chunk = raw[pos:pos + width]
            pos += width
            regs.append(int(chunk, 16) if len(chunk) == width and "x" not in chunk else None)
        return regs

    def read_mem(self, addr: int, size: int) -> Optional[bytes]:
        r = self.cmd(f"m{addr:x},{size:x}")
        if r.startswith("E") or len(r) < 2 * size:
            return None
        return bytes.fromhex(r[:2 * size])

    def cont(self) -> None:
        self.send("vCont;c")

    def interrupt(self) -> None:
        self.sock.sendall(b"\x03")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def decode(word: int, regs: List[Optional[int]]) -> dict:
    op = word >> 26
    if op in D_FORM:
        ra = (word >> 16) & 31
        d = word & 0xFFFF
        d = d - 0x10000 if d & 0x8000 else d
        base = regs[ra] if ra else 0
        ea = ((base or 0) + d) & 0xFFFFFFFF if base is not None else None
        return {"text": f"{D_FORM[op]} {(word >> 21) & 31}, {d:#x}(r{ra})", "ea": ea,
                "store": D_FORM[op].startswith("st"), "base_reg": ra}
    return {"text": f".long 0x{word:08X}", "ea": None, "store": False, "base_reg": None}


def load_addrs(args) -> List[int]:
    addrs = [int(a, 0) for a in (args.addr or [])]
    if args.findings:
        with open(args.findings, "r", encoding="utf-8") as f:
            for item in json.load(f).get("items", []):
                if args.category and item.get("category") != args.category:
                    continue
                c = item.get("comment", "")
                # instruction-level findings only; function labels are not breakpoint targets
                if item.get("label") or (args.stores_only and " st" not in c):
                    continue
                addrs.append(int(item["addr"], 16))
    return sorted(set(addrs))[:args.limit]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--addr", action="append", help="instruction address to break on (repeatable)")
    ap.add_argument("--findings", help="findings file from ps3elf.py --ghidra")
    ap.add_argument("--category", default="field")
    ap.add_argument("--stores-only", action="store_true", help="from a findings file take stores only")
    ap.add_argument("--watch", action="append", default=[], help="data address of interest (repeatable)")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--limit", type=int, default=64, help="max breakpoints")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2345)
    ap.add_argument("-o", "--output", default="gdbtrace.json")
    args = ap.parse_args(argv)

    addrs = load_addrs(args)
    if not addrs:
        print("нет адресов: задайте --addr или --findings")
        return 2
    watch = {int(w, 0) for w in args.watch}
    try:
        gdb = GdbClient(args.host, args.port)
    except GdbError as e:
        print(f"ошибка: {e}")
        return 1
    hits: List[dict] = []
    active = set()
    try:
        for a in addrs:
            gdb.set_bp(a)
            active.add(a)
        print(f"поставлено брейкпоинтов: {len(active)}. Игра продолжена на {args.seconds:.0f} c - "
              f"вращайте камеру.", flush=True)
        end = time.time() + args.seconds
        while active and time.time() < end:
            gdb.cont()
            reply = gdb.recv(max(0.1, end - time.time()))
            if reply is None:                    # nothing hit in time: stop the game to get control back
                gdb.interrupt()
                gdb.recv(10.0)
                break
            tid = gdb.current_thread()
            if tid is not None:
                gdb.select_thread(tid)
            regs = gdb.registers()
            pc = regs[REG_PC]
            if pc is None or (pc & 0xFFFFFFFF) not in active:
                continue                         # paused for some other reason
            pc &= 0xFFFFFFFF
            raw = gdb.read_mem(pc, 4)
            info = decode(struct.unpack(">I", raw)[0], regs) if raw else {"text": "?", "ea": None, "store": False}
            hit = {"pc": f"0x{pc:08X}", "thread": f"0x{tid:X}" if tid is not None else "?",
                   "lr": f"0x{(regs[REG_LR] or 0) & 0xFFFFFFFF:08X}", "insn": info["text"],
                   "ea": f"0x{info['ea']:08X}" if info.get("ea") is not None else None,
                   "watched": bool(info.get("ea") in watch)}
            hits.append(hit)
            mark = "  <== пишет/читает наблюдаемый адрес" if hit["watched"] else ""
            print(f"  {hit['pc']}  {hit['insn']:24s} EA {hit['ea']}  LR {hit['lr']}  поток {hit['thread']}{mark}",
                  flush=True)
            gdb.clear_bp(pc)                     # one shot: the stub would re-hit it immediately otherwise
            active.discard(pc)
    except GdbError as e:
        print(f"ошибка GDB: {e}")
    finally:
        for a in list(active):
            try:
                gdb.clear_bp(a)
            except GdbError:
                break
        gdb.close()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"tool": "gdbtrace", "breakpoints": [f"0x{a:08X}" for a in addrs], "hits": hits}, f, indent=1)
    silent = [a for a in addrs if f"0x{a:08X}" not in {h["pc"] for h in hits}]
    print(f"\nсработало {len(hits)} из {len(addrs)}; результат в {args.output}")
    if not hits:
        print("Ни одного срабатывания: проверьте, что PPU Decoder = Interpreter (static), и что камера двигалась.")
    elif silent:
        print("не выполнялись: " + ", ".join(f"0x{a:08X}" for a in silent[:16]))
    print("RPCS3 остался на паузе и записал в лог fatal error потока GDB Server - это ожидаемо. "
          "Нажмите Resume; для нового сеанса GDB перезапустите игру.")
    return 0 if hits else 2


if __name__ == "__main__":
    sys.exit(main())
