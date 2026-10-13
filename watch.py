#!/usr/bin/env python3
"""watch - inspect, log and poke guest memory of a game running in RPCS3.

    watch.py info                       game identity, backend, memory map
    watch.py selftest [--write]         cross-check the process and PINE backends (--write: test writing)
    watch.py watch SPEC [SPEC ...]      live values; --csv FILE logs every sample
    watch.py poke  SPEC VALUE [--add]   write one float (or --u32)
    watch.py hold  --set SPEC VALUE ... keep writing values for --seconds (what a profile's writes do)
    watch.py struct SPEC                float/hex/pointer view around an address,
                                        marking the words that change while you play
    watch.py find --u32 V[-V2] | --f32 V   exact-value search (also on a dump)

SPEC is an address (0x30251XXX), a named address (yaw=0x30251XXX), or a pointer
chain in KAMI's DerefChain form:  [0x00A12340]+0x5C+0x118+0x1A8
(read32(0x00A12340)+0x5C -> read32(..)+0x118 -> read32(..)+0x1A8).
--chains chains.json [--index N] adds the fields of a chain saved by ptrscan.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import struct
import sys
import time
from typing import List, Optional, Tuple

from rpcs3mem import add_backend_args, open_backend, parse_int, parse_range, pine_info, resolve_chain
from rpcs3mem.common import MemoryError_


class Spec:
    def __init__(self, text: str):
        self.name, body = (text.split("=", 1) + [None])[:2] if "=" in text else (None, text)
        body = (body or text).strip()
        self.base: int
        self.offsets: List[int] = []
        if body.startswith("["):
            head, _, tail = body[1:].partition("]")
            self.base = parse_int(head)
            self.offsets = [parse_int(t) for t in tail.split("+") if t.strip()]
            if not self.offsets:
                raise ValueError(f"chain {text!r} needs at least one +offset after the bracket")
        else:
            self.base = parse_int(body)
        self.name = self.name or body

    def resolve(self, mem) -> Optional[int]:
        return resolve_chain(mem, self.base, self.offsets) if self.offsets else self.base


def specs_from_args(args) -> List[Spec]:
    specs = [Spec(s) for s in (args.spec or [])]
    if getattr(args, "chains", None):
        with open(args.chains, "r", encoding="utf-8") as f:
            doc = json.load(f)
        d = doc["chains"][args.index]
        for name, off in d["fields"].items():
            specs.append(Spec(f"{name}=[{d['base']}]+" + "+".join(d["offsets"] + [off])))
    if not specs:
        raise SystemExit("nothing to watch: give an address/chain or --chains FILE")
    return specs


def read_cell(mem, addr: Optional[int]) -> Tuple[Optional[float], Optional[int]]:
    if addr is None:
        return None, None
    try:
        raw = mem.read(addr, 4)
    except MemoryError_:
        return None, None
    return struct.unpack(">f", raw)[0], struct.unpack(">I", raw)[0]


def fmt_float(f: Optional[float]) -> str:
    if f is None:
        return "      ----"
    if not math.isfinite(f) or abs(f) >= 1e7 or (f != 0 and abs(f) < 1e-5):
        return f"{f:10.3e}"
    return f"{f:10.5f}"


# --------------------------------------------------------------------------
def cmd_info(args) -> int:
    mem = open_backend(args)
    try:
        info = mem.game_info() or pine_info(args.pine)
        print(f"backend : {mem.name}" + (f" (pid {mem.pid}, base 0x{mem.base:X})" if mem.name == "process" else ""))
        for k in ("emulator", "title", "id", "version", "hash"):
            print(f"{k:8s}: {info.get(k, '?')}")
        if not info:
            print("          (identity needs RPCS3 IPC: enable it, and close KAMI while asking)")
        regs = mem.regions()
        print(f"regions : {len(regs)}, {sum(r.size for r in regs) / 1048576:.0f} MiB")
        for r in regs:
            print("   " + str(r))
    finally:
        mem.close()
    return 0


def cmd_selftest(args) -> int:
    """Read the same memory through both live backends and compare."""
    from rpcs3mem.pine import PineClient, PineError, PineFail
    from rpcs3mem.procmem import ProcessMemory
    ok = write_ok = True
    try:
        pm = ProcessMemory(args.pid, args.base)
        print(f"process : pid {pm.pid}, guest base 0x{pm.base:X}")
    except (MemoryError_, OSError) as e:
        print(f"process : FAILED - {e}")
        return 1
    regs = [r for r in pm.regions() if r.start < 0xC0000000]
    print(f"regions : {len(regs)}, {sum(r.size for r in regs) / 1048576:.0f} MiB below 0xC0000000")
    head = pm.read(0x10000, 4)
    print(f"0x10000 : {head.hex()} {'(ELF header, as expected)' if head == bytes.fromhex('7f454c46') else '(no ELF magic here)'}")
    try:
        with PineClient(args.pine, timeout=2.0) as c:
            print(f"pine    : {c.version()} | {c.game_id()} {c.game_version()} | {c.game_uuid()}")
            bad = unmapped = 0
            probes = [r.start + (r.size // 2 & ~7) for r in regs[:64]]
            for a in probes:
                via_proc = struct.unpack(">Q", pm.read(a, 8))[0]
                try:
                    via_pine = c.read(a, 8)
                    if via_proc != via_pine:
                        # memory is live; one retry tells a real mismatch from a value that just changed
                        if struct.unpack(">Q", pm.read(a, 8))[0] != c.read(a, 8):
                            bad += 1
                except PineFail:
                    # IPC works, but this page is not allocated in the game's own view (the host
                    # mapping of a big region can be wider than what the guest has allocated)
                    unmapped += 1
            print(f"compare : {len(probes) - bad - unmapped}/{len(probes) - unmapped} sampled addresses agree "
                  f"between process and PINE" + (f"; {unmapped} not allocated in the guest (PINE refuses, normal)"
                                                 if unmapped else ""))
            ok = bad == 0
    except PineError as e:
        print(f"pine    : not available - {e}")
    if args.write:
        # Rewrite the first word of the ELF header with its own content: nothing changes in the game,
        # but it proves that WriteProcessMemory / the host page protection let us write at all.
        try:
            before = pm.read(0x10000, 4)
            pm.write(0x10000, before)
            after = pm.read(0x10000, 4)
            if after == before:
                print("write   : OK (4 bytes at 0x10000 rewritten with their own content and read back)")
            else:
                print(f"write   : FAILED - wrote {before.hex()}, read back {after.hex()}")
                write_ok = False
        except (MemoryError_, OSError) as e:
            print(f"write   : FAILED - {e}")
            print("          camfind's write test (phase D) cannot work. Run the terminal as administrator "
                  "(required if RPCS3 runs elevated) or use --backend pine.")
            write_ok = False
    pm.close()
    if not ok:
        print("RESULT  : MISMATCH - do not trust the process backend; pass --base or use --backend pine")
    elif not write_ok:
        print("RESULT  : READ OK, WRITE FAILED - camfind can search, but its write test and the mouse cannot work")
    else:
        print("RESULT  : OK")
    return 0 if ok and write_ok else 1


def cmd_watch(args) -> int:
    specs = specs_from_args(args)
    mem = open_backend(args)
    writer = None
    fh = None
    if args.csv:
        fh = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["time_s"] + [f"{s.name}" for s in specs] + [f"{s.name}_addr" for s in specs] +
                        [f"{s.name}_u32" for s in specs])           # exact integer value (flags / modes read as floats are denormals)
    t0 = time.time()
    last_line = ""
    try:
        while True:
            now = time.time() - t0
            addrs = [s.resolve(mem) for s in specs]
            cells = [read_cell(mem, a) for a in addrs]
            line = "  ".join(f"{s.name}@{'????????' if a is None else '%08X' % a}={fmt_float(c[0]).strip()}"
                             for s, a, c in zip(specs, addrs, cells))
            if writer:
                writer.writerow([f"{now:.4f}"] + ["" if c[0] is None else repr(c[0]) for c in cells] +
                                ["" if a is None else f"0x{a:08X}" for a in addrs] +
                                ["" if c[1] is None else str(c[1]) for c in cells])
            if not args.quiet and (line != last_line or args.all):
                print(f"{now:8.2f}  {line}", flush=True)
                last_line = line
            if args.duration and now >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if fh:
            fh.close()
        mem.close()
    return 0


def _beep(n: int = 1) -> None:
    try:
        import winsound
        for _ in range(n):
            winsound.Beep(1000, 150)
    except Exception:
        pass


def cmd_poke(args) -> int:
    spec = Spec(args.spec)
    mem = open_backend(args, need_write=True)
    try:
        if args.delay > 0:
            print(f"запись через {args.delay:.0f} с: перейдите в игру и приведите её в нужное состояние "
                  f"(например, зажмите прицел)...", flush=True)
            time.sleep(args.delay)
            _beep(1)
        addr = spec.resolve(mem)
        if addr is None:
            print("chain does not resolve")
            return 2
        raw = mem.read(addr, 4)
        if args.u32:
            old = struct.unpack(">I", raw)[0]
            new = (old + parse_int(args.value)) if args.add else parse_int(args.value)
            mem.write(addr, struct.pack(">I", new & 0xFFFFFFFF))
            print(f"0x{addr:08X}: 0x{old:08X} -> 0x{new & 0xFFFFFFFF:08X}")
        else:
            old = struct.unpack(">f", raw)[0]
            new = old + float(args.value) if args.add else float(args.value)
            mem.write(addr, struct.pack(">f", new))
            time.sleep(args.wait)
            now = struct.unpack(">f", mem.read(addr, 4))[0]
            delta = abs(new - old)
            if abs(now - new) <= max(1e-3, 0.1 * delta):
                verdict = "держится"
            elif abs(now - old) <= max(1e-3, 0.05 * min(delta, 1.0)):
                verdict = "игра вернула прежнее значение"
            else:
                verdict = f"ушло к другому значению {now:.6f}: игра ограничила или пересчитала его"
            print(f"0x{addr:08X}: {old:.6f} -> {new:.6f}; через {args.wait:.2f} c: {now:.6f} ({verdict})")
            if args.revert:
                mem.write(addr, struct.pack(">f", old))
                _beep(2)
                print(f"возвращено прежнее значение {old:.6f}")
    finally:
        mem.close()
    return 0


def cmd_hold(args) -> int:
    """Write the same values over and over, like a profile's "writes" do while KAMI injects: finds out whether
    forcing a field (say a "stick is held" flag) stops something the game does on its own."""
    items = []
    for spec_text, val in args.set:
        kind, _, body = val.partition(":") if ":" in val else ("f32", "", val)
        if kind not in ("f32", "u32"):
            raise SystemExit(f"значение {val!r}: ожидается число, f32:число или u32:число")
        v = parse_int(body) & 0xFFFFFFFF if kind == "u32" else float(body)
        items.append((Spec(spec_text), kind, v))
    mem = open_backend(args, need_write=True)
    try:
        for spec, kind, v in items:
            print(f"{spec.name} -> 0x{spec.resolve(mem) or 0:08X}  {kind} {v}")
        period = 1.0 / args.rate
        end = time.time() + args.seconds
        n = 0
        print(f"пишу {len(items)} знач. {args.rate:.0f} раз/с в течение {args.seconds:.0f} с ... (Ctrl+C - стоп)")
        try:
            while time.time() < end:
                t0 = time.time()
                for spec, kind, v in items:
                    addr = spec.resolve(mem)
                    if addr is not None:
                        mem.write(addr, struct.pack(">I", v) if kind == "u32" else struct.pack(">f", v))
                n += 1
                dt = period - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
        except KeyboardInterrupt:
            pass
        print(f"готово: {n} циклов записи")
    finally:
        mem.close()
    return 0


def cmd_struct(args) -> int:
    spec = Spec(args.spec)
    mem = open_backend(args)
    try:
        centre = spec.resolve(mem)
        if centre is None:
            print("chain does not resolve")
            return 2
        start = (centre - args.before) & ~3
        size = (args.before + args.after + 3) & ~3
        regions = mem.regions()

        def mapped(v: int) -> bool:
            return any(v in r for r in regions) and v >= 0x10000

        first = mem.read(start, size)
        changed = bytearray(size // 4)
        lo = [struct.unpack_from(">f", first, i)[0] for i in range(0, size, 4)]
        hi = list(lo)
        if args.seconds > 0:
            print(f"наблюдаю {args.seconds:.0f} c - подвигайте камеру / персонажа ...", file=sys.stderr)
            end = time.time() + args.seconds
            while time.time() < end:
                cur = mem.read(start, size)
                for i in range(0, size, 4):
                    if cur[i:i + 4] != first[i:i + 4]:
                        changed[i // 4] = 1
                        f = struct.unpack_from(">f", cur, i)[0]
                        if math.isfinite(f):
                            k = i // 4
                            lo[k], hi[k] = min(lo[k], f), max(hi[k], f)
                time.sleep(0.05)
        data = mem.read(start, size)
        print(f"   адрес     смещ.    hex        float       заметки")
        for i in range(0, size, 4):
            a = start + i
            u = struct.unpack_from(">I", data, i)[0]
            f = struct.unpack_from(">f", data, i)[0]
            notes = []
            if changed[i // 4]:
                notes.append(f"МЕНЯЛОСЬ [{lo[i // 4]:.4f} .. {hi[i // 4]:.4f}]")
            if mapped(u):
                notes.append("указатель")
            if args.changed_only and not changed[i // 4]:
                continue
            mark = ">>" if a == centre else "  "
            print(f"{mark} {a:08X}  {a - centre:+6X}  {u:08X}  {fmt_float(f)}   {'; '.join(notes)}")
    finally:
        mem.close()
    return 0


def cmd_find(args) -> int:
    """Exact-value search over all scanned memory (works on a dump too)."""
    import numpy as np
    from rpcs3mem.scan import read_region, scan_regions
    mem = open_backend(args)
    try:
        lo, hi = args.range
        total = 0
        for r in scan_regions(mem, lo, hi):
            words = read_region(mem, r).view(">u4")
            if args.u32:
                a, _, b = args.u32.partition("-")
                vlo, vhi = parse_int(a), parse_int(b or a)
                idx = np.flatnonzero((words >= vlo) & (words <= vhi))
            else:
                f = words.view(">f4")
                with np.errstate(invalid="ignore"):
                    idx = np.flatnonzero(np.abs(f - np.float32(args.f32)) <= args.tol)
            for i in idx[:max(0, args.limit - total)].tolist():
                u = int(words[i])
                print(f"0x{r.start + 4 * i:08X}  {u:08X}  {fmt_float(struct.unpack('>f', struct.pack('>I', u))[0])}")
            total += len(idx)
        print(f"найдено: {total}" + (f" (показано {args.limit})" if total > args.limit else ""))
    finally:
        mem.close()
    return 0 if total else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("find", help="search memory for a value")
    add_backend_args(p)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--u32", metavar="V[-V2]", help="32-bit value or inclusive range, e.g. a vtable address "
                                                    "to find objects of a class: --u32 0x00812300-0x008123FF")
    g.add_argument("--f32", type=float, help="float value")
    p.add_argument("--tol", type=float, default=1e-6)
    p.add_argument("--range", type=parse_range, default=(0x10000, 0xC0000000), metavar="LO-HI")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("info", help="game identity and memory map")
    add_backend_args(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("selftest", help="check that process access and PINE agree")
    add_backend_args(p)
    p.add_argument("--write", action="store_true",
                   help="also check that writing works (rewrites 4 bytes at 0x10000 with their own content)")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("watch", help="print / log values")
    add_backend_args(p)
    p.add_argument("spec", nargs="*")
    p.add_argument("--chains")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--interval", type=float, default=0.1)
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    p.add_argument("--csv", help="log every sample to this file")
    p.add_argument("--all", action="store_true", help="print every sample, not only changes")
    p.add_argument("--quiet", action="store_true", help="no console output (with --csv)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("poke", help="write a value and see whether the game keeps it")
    add_backend_args(p)
    p.add_argument("spec")
    p.add_argument("value")
    p.add_argument("--add", action="store_true", help="add VALUE to the current value")
    p.add_argument("--u32", action="store_true", help="write an integer instead of a float")
    p.add_argument("--wait", type=float, default=0.3)
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds before writing (time to get the game into the right state); beeps when it writes")
    p.add_argument("--revert", action="store_true", help="put the old value back after --wait (floats only)")
    p.set_defaults(func=cmd_poke)

    p = sub.add_parser("hold", help="keep writing fixed values (what a profile's writes do) for some seconds")
    add_backend_args(p)
    p.add_argument("--set", nargs=2, action="append", required=True, metavar=("SPEC", "VALUE"),
                   help="field and value: a number (float), f32:1.5 or u32:1; repeatable")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--rate", type=float, default=500.0, help="write cycles per second")
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("struct", help="view the memory around an address")
    add_backend_args(p)
    p.add_argument("spec")
    p.add_argument("--before", type=parse_int, default=0x40)
    p.add_argument("--after", type=parse_int, default=0x100)
    p.add_argument("--seconds", type=float, default=0, help="watch this long and mark the words that change")
    p.add_argument("--changed-only", action="store_true")
    p.set_defaults(func=cmd_struct)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except MemoryError_ as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
