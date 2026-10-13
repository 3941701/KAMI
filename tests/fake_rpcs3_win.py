#!/usr/bin/env python3
"""Windows port of tests/fake_rpcs3.py (which needs Linux: memfd_create / mmap).

It reuses the simulated game and PINE server of fake_rpcs3.py unchanged and only swaps the
memory backing: every guest block is a pagefile-backed file mapping, viewed twice at the same
host addresses as in RPCS3 (guest view at 0x3_0000_0000 + addr, read/write "sudo" mirror 4 GiB
above), so the toolkit's real Windows code (ReadProcessMemory / WriteProcessMemory /
VirtualQueryEx) is exercised against it.  Always pass --pid to the tools: never let them pick
"rpcs3.exe" on a machine where a real RPCS3 may be running.

Two extras make the simulated game nastier, to reproduce what a real game does to camfind:

  --decoys N   N low-address words that follow the smoothed yaw exactly like an angle does but are
               rewritten from it every frame (look-alikes that are not the master).  The old
               write test tried candidates in address order and gave up after 120.
  glitch       control command "glitch 1": N yaw-derived words start to flip between NaN, +-inf,
               +-3e38 and a normal number, as pointers / integers / garbage do in a live game.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if os.name != "nt":
    raise SystemExit("fake_rpcs3_win.py is for Windows; use fake_rpcs3.py on Linux")

_real_cdll = ctypes.CDLL


class _NoLibc:
    def __getattr__(self, name):
        return self


ctypes.CDLL = lambda *a, **k: _NoLibc()           # fake_rpcs3 loads libc at import time (Linux only)
import fake_rpcs3 as fake                          # noqa: E402
ctypes.CDLL = _real_cdll

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileMappingW.restype = ctypes.c_void_p
k32.CreateFileMappingW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_wchar_p]
k32.MapViewOfFileEx.restype = ctypes.c_void_p
k32.MapViewOfFileEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_size_t, ctypes.c_void_p]
PAGE_READWRITE, FILE_MAP_WRITE, FILE_MAP_READ = 0x04, 0x02, 0x04
INVALID_HANDLE = ctypes.c_void_p(-1).value


class WinGuest(fake.Guest):
    """Guest memory as file-mapping blocks viewed at BASE+addr and SUDO+addr."""

    def __init__(self):
        self.blocks = []
        self.pages = set()
        self.handles = []

    def map(self, start, size, writable=True, executable=False, name="blk"):
        h = k32.CreateFileMappingW(INVALID_HANDLE, None, PAGE_READWRITE, size >> 32, size & 0xFFFFFFFF, None)
        if not h:
            raise OSError(f"CreateFileMapping({size:#x}) failed: {ctypes.get_last_error()}")
        guest = k32.MapViewOfFileEx(h, FILE_MAP_WRITE if writable else FILE_MAP_READ, 0, 0, size,
                                    fake.BASE + start)
        sudo = k32.MapViewOfFileEx(h, FILE_MAP_WRITE, 0, 0, size, fake.SUDO + start)
        if guest != fake.BASE + start or sudo != fake.SUDO + start:
            raise OSError(f"MapViewOfFileEx at guest {start:#x} failed: {ctypes.get_last_error()} "
                          f"(got {guest}, {sudo})")
        self.handles.append(h)
        py = (ctypes.c_char * size).from_address(sudo)     # slices read/write as bytes
        self.blocks.append((start, size, py, writable, executable))
        self.pages.update(range(start >> 12, (start + size) >> 12))
        return py


fake.Guest = WinGuest


class WinGame(fake.Game):
    def __init__(self, seed: int, model: str, decoys: int = 0, glitchers: int = 300):
        super().__init__(seed, model)
        rng = self.rng
        # look-alikes of the yaw angle in low static data (below the heap, so first in address order)
        self.decoys = [(0x00800000 + 0x40 * k, rng.uniform(-2.5, 2.5)) for k in range(decoys)]
        lo, hi = 0x31000000, 0x31800000
        self.glitch = [(lo + 4 * rng.randrange((hi - lo) // 4), rng.uniform(0.5, 3), rng.uniform(0, 6),
                        rng.uniform(0.5, 50)) for _ in range(glitchers)]
        self.glitch_on = False

    def step(self, dt):
        super().step(dt)
        if not hasattr(self, "glitch"):               # the first frame is drawn inside _build()
            return
        g, ys = self.g, self.yaw_s
        for a, off in self.decoys:
            g.wf(a, fake.wrap_pi(ys + off))
        bad = (float("nan"), float("inf"), 3.0e38, -3.0e38, -float("inf"))
        for n, (a, k, ph, amp) in enumerate(self.glitch):
            if self.glitch_on and (self.frame + n) % 3:
                g.wf(a, bad[(self.frame + n) % len(bad)])
            else:
                g.wf(a, math.sin(ys * k + ph) * amp)


def control_server(game: WinGame, listener: socket.socket, stop: threading.Event):
    """Like fake_rpcs3.control_server, but one thread per connection: camfind's stimulus keeps its
    connection open for the whole run and the test harness needs to send commands meanwhile."""
    def serve(conn):
        f = conn.makefile("rw")
        try:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "stick":
                    with game.lock:
                        game.stick = [float(x) for x in parts[1:5]]
                    f.write("ok\n")
                elif parts[0] == "get":
                    f.write(json.dumps(game.truth()) + "\n")
                elif parts[0] == "glitch":
                    game.glitch_on = parts[1] == "1"
                    f.write("ok\n")
                elif parts[0] == "limits":              # narrow the pitch window at run time: the game clamps writes
                    game.PITCH_MIN, game.PITCH_MAX = float(parts[1]), float(parts[2])
                    f.write("ok\n")
                elif parts[0] == "quit":
                    f.write("ok\n")
                    f.flush()
                    stop.set()
                    break
                f.flush()
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    while not stop.is_set():
        conn, _ = listener.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--control-port", type=int, default=28500)
    ap.add_argument("--pine-port", type=int, default=28501)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--model", choices=("hava", "hvecva", "hvvec"), default="hava")
    ap.add_argument("--decoys", type=int, default=0)
    ap.add_argument("--glitchers", type=int, default=300)
    args = ap.parse_args()

    game = WinGame(args.seed, args.model, args.decoys, args.glitchers)
    stop = threading.Event()
    pine_l = socket.socket()
    pine_l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    pine_l.bind(("127.0.0.1", args.pine_port))
    pine_l.listen(16)
    ctl_l = socket.socket()
    ctl_l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctl_l.bind(("127.0.0.1", args.control_port))
    ctl_l.listen(4)
    threading.Thread(target=fake.pine_server, args=(game, pine_l), daemon=True).start()
    threading.Thread(target=control_server, args=(game, ctl_l, stop), daemon=True).start()
    print(json.dumps({"ready": True, "pid": os.getpid(), **game.truth()}), flush=True)
    dt = 1.0 / args.fps
    nxt = time.perf_counter()
    while not stop.is_set():
        game.step(dt)
        nxt += dt
        delay = nxt - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        else:
            nxt = time.perf_counter()
    return 0


if __name__ == "__main__":
    sys.exit(main())
