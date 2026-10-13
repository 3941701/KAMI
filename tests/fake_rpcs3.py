#!/usr/bin/env python3
"""A stand-in for RPCS3 running a small third-person "game", for testing the toolkit.

It reproduces the parts of RPCS3 the tools depend on, modelled on the RPCS3 source:

* guest memory mapped at host 0x3_0000_0000 with the read/write "sudo" mirror
  4 GiB above it (vm.cpp), every guest block backed by shared memory;
* a PINE server with RPCS3's behaviour: one client at a time, batched packets,
  a whole batch failing on one unmapped address, request/reply size limits and
  the MsgStatus argument quirk (3rdparty/pine/pine_server.h);
* "Dump Guest Memory" output (manifest.json + sparse guest_memory.bin).

The game itself is deliberately awkward in the ways real ones are: the camera
object sits at a different heap address every session, the master yaw/pitch
are surrounded by derived copies (smoothed angles, direction vector, degrees,
view matrices) that the game rewrites every frame, there is an inactive second
camera, a pad buffer, timers, per-frame noise, and several pointer paths to
the camera of which only some survive a restart.

Linux only.  Not part of the toolkit proper - nothing here is needed on the
user's machine.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import math
import mmap
import os
import random
import socket
import struct
import sys
import threading
import time

BASE = 0x3_0000_0000
SUDO = BASE + 0x1_0000_0000
PROT_NONE, PROT_READ, PROT_WRITE = 0, 1, 2
MAP_SHARED, MAP_PRIVATE, MAP_FIXED, MAP_ANONYMOUS = 0x01, 0x02, 0x10, 0x20
MAP_NORESERVE, MAP_FIXED_NOREPLACE = 0x4000, 0x100000

libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype = ctypes.c_void_p
libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                      ctypes.c_int, ctypes.c_long]


def _mmap(addr, size, prot, flags, fd=-1):
    res = libc.mmap(addr, size, prot, flags, fd, 0)
    if res in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_errno(), f"mmap({addr:#x}, {size:#x}) failed")
    if addr and res != addr:
        raise OSError(f"mmap landed at {res:#x}, wanted {addr:#x}")
    return res


class Guest:
    """Guest memory: named shared-memory blocks mapped at BASE+addr and SUDO+addr."""

    def __init__(self):
        _mmap(BASE, 0x2_0000_0000, PROT_NONE,
              MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE | MAP_FIXED_NOREPLACE)
        self.blocks = []            # (start, size, mmap, writable, executable)
        self.pages = set()

    def map(self, start, size, writable=True, executable=False, name="blk"):
        fd = os.memfd_create(f"rpcs3_{name}")
        os.ftruncate(fd, size)
        py = mmap.mmap(fd, size)
        _mmap(BASE + start, size, PROT_READ | (PROT_WRITE if writable else 0), MAP_SHARED | MAP_FIXED, fd)
        _mmap(SUDO + start, size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_FIXED, fd)
        os.close(fd)
        self.blocks.append((start, size, py, writable, executable))
        self.pages.update(range(start >> 12, (start + size) >> 12))
        return py

    def _find(self, addr, size):
        for start, bsize, py, _w, _x in self.blocks:
            if start <= addr and addr + size <= start + bsize:
                return py, addr - start
        raise KeyError(f"unmapped guest address {addr:#x}")

    def mapped(self, addr, size=1):
        return (addr >> 12) in self.pages and ((addr + size - 1) >> 12) in self.pages

    def page_writable(self, addr):
        for start, bsize, _py, w, _x in self.blocks:
            if start <= addr < start + bsize:
                return w
        return False

    def read(self, addr, size):
        py, off = self._find(addr, size)
        return py[off:off + size]

    def write(self, addr, data):
        py, off = self._find(addr, len(data))
        py[off:off + len(data)] = data

    def r32(self, a): return struct.unpack(">I", self.read(a, 4))[0]
    def w32(self, a, v): self.write(a, struct.pack(">I", v & 0xFFFFFFFF))
    def rf(self, a): return struct.unpack(">f", self.read(a, 4))[0]
    def wf(self, a, v): self.write(a, struct.pack(">f", v))


def wrap_pi(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


class Game:
    TITLE, TITLE_ID, VERSION = "inFAMOUS (simulated)", "BCUS98119", "01.00"
    HASH = "PPU-412a9bf323b4743de21658a286f71dbe82cad807"

    G_GAME = 0x00A12340           # static: -> GameManager
    G_RENDER = 0x00A30000         # static: -> RenderView (-> camera matrix)
    G_LIST = 0x00A40000           # static: -> object list (camera at a session-dependent index)
    PAD_BUF = 0x00A20000          # CellPadData
    TIME = 0x00A50000
    STATIC_VIEW = 0x00B00000      # renderer's copy of the view matrix
    OFF_CAMMGR, OFF_PLAYER = 0x5C, 0x60
    OFF_ACTIVE_CAM, OFF_SECOND_CAM = 0x118, 0x11C
    YAW, PITCH, DIST, FOV = 0x1A8, 0x1C4, 0x1D0, 0x1D8
    YAW_S, PITCH_S, YAW_DEG = 0x200, 0x204, 0x210
    DIR, HVEC, MATRIX, POS = 0x60, 0x70, 0x80, 0x10
    PITCH_MIN, PITCH_MAX = -1.0, 1.2
    YAW_SPEED, PITCH_SPEED = 2.5, 1.5

    def __init__(self, seed: int, model: str = "hava"):
        # which values the game treats as the camera's master state (KAMI's three camera models):
        #   hava   - yaw and pitch scalars
        #   hvecva - a sin/cos pair for yaw, a scalar for pitch
        #   hvvec  - one 3D direction vector
        self.model = model
        self.rng = random.Random(seed)
        self.g = Guest()
        self.stick = [0.0, 0.0, 0.0, 0.0]          # rx, ry, lx, ly  (ry > 0 = stick up)
        self.frame = 0
        self.lock = threading.Lock()
        self._build()

    # -- memory image -----------------------------------------------------
    def _build(self):
        g, rng = self.g, self.rng
        text = g.map(0x00010000, 0x00800000 - 0x10000, writable=False, executable=True, name="text")
        g.map(0x00800000, 0x00400000, writable=True, name="data")
        heap = g.map(0x30000000, 0x02000000, name="heap")
        g.map(0x20000000, 0x00400000, name="user")
        g.map(0xC0000000, 0x01000000, name="video")
        g.map(0xD0000000, 0x00100000, name="stack")

        # ELF64 big-endian header + 2 PT_LOAD so tools can find the static data segment
        ehdr = struct.pack(">4sBBBBB7xHHIQQQIHHHHHH", b"\x7fELF", 2, 2, 1, 0x66, 0, 2, 21, 1,
                           0x00A00000, 64, 0, 0, 64, 56, 2, 64, 0, 0)
        ph_text = struct.pack(">IIQQQQQQ", 1, 5, 0, 0x10000, 0x10000, 0x7F0000, 0x7F0000, 0x10000)
        ph_data = struct.pack(">IIQQQQQQ", 1, 6, 0x7F0000, 0x800000, 0x800000, 0x300000, 0x400000, 0x10000)
        text[0:len(ehdr) + 112] = ehdr + ph_text + ph_data
        # some constants in "rodata", including pi
        for i, v in enumerate((math.pi, 2 * math.pi, math.pi / 180, 1.0, 0.5)):
            g.wf(0x00780000 + 4 * i, v)

        # heap background: plausible floats and plausible-looking pointers
        import numpy as np
        nrng = np.random.default_rng(self.rng.randrange(1 << 30))
        floats = nrng.uniform(-10, 10, 0x100000).astype(">f4").tobytes()
        heap[0x1000000:0x1000000 + len(floats)] = floats
        ptrs = (0x30000000 + nrng.integers(0, 0x01000000 // 4, 0x40000) * 4).astype(">u4").tobytes()
        heap[0x1800000:0x1800000 + len(ptrs)] = ptrs

        # session-dependent allocations
        cur = 0x30000000 + 0x10000 * rng.randrange(1, 48)

        def alloc(size):
            nonlocal cur
            cur += 0x10 * rng.randrange(1, 64)
            a = cur
            cur += (size + 0xF) & ~0xF
            return a

        self.game_mgr = alloc(0x200)
        self.player = alloc(0x200)
        self.cam_mgr = alloc(0x400)
        self.cam2 = alloc(0x400)
        self.cam = alloc(0x400)
        self.render = alloc(0x100)
        self.objlist = alloc(0x400)

        g.w32(self.G_GAME, self.game_mgr)
        g.w32(self.G_GAME + 4, self.player)
        g.w32(self.game_mgr + self.OFF_CAMMGR, self.cam_mgr)
        g.w32(self.game_mgr + self.OFF_PLAYER, self.player)
        g.w32(self.cam_mgr + self.OFF_ACTIVE_CAM, self.cam)
        g.w32(self.cam_mgr + self.OFF_SECOND_CAM, self.cam2)
        g.w32(self.G_RENDER, self.render)
        g.w32(self.render + 0x20, self.cam + self.MATRIX)
        g.w32(self.G_LIST, self.objlist)
        self.list_index = rng.randrange(4, 60)       # unstable path: differs per session
        g.w32(self.objlist + 4 * self.list_index, self.cam)
        for c in (self.cam, self.cam2):
            g.w32(c, 0x00781000)                      # "vtable"
            g.wf(c + self.DIST, 4.5)
            g.wf(c + self.FOV, 1.0)
        g.wf(self.cam2 + self.YAW, 0.75)             # inactive camera keeps its own angles
        g.wf(self.cam2 + self.PITCH, -0.2)

        self.yaw_s, self.pitch_s = 0.3, 0.1
        g.wf(self.cam + self.YAW, 0.3)
        g.wf(self.cam + self.PITCH, 0.1)
        g.write(self.cam + self.HVEC, struct.pack(">2f", math.sin(0.3), math.cos(0.3)))
        g.write(self.cam + self.DIR, struct.pack(">3f", math.cos(0.1) * math.cos(0.3), math.sin(0.1),
                                                 math.cos(0.1) * math.sin(0.3)))
        self.pos = [10.0, 0.0, -4.0]

        # per-frame noise, and values derived from yaw / pitch / position elsewhere in memory
        def scatter(n, lo=0x30800000, hi=0x30F00000):
            return [lo + 4 * rng.randrange((hi - lo) // 4) for _ in range(n)]

        self.noise = scatter(256)
        self.yaw_derived = [(a, rng.uniform(0.5, 3), rng.uniform(0, 6), rng.uniform(0.5, 50)) for a in scatter(400)]
        self.pitch_derived = [(a, rng.uniform(0.5, 3), rng.uniform(0, 6), rng.uniform(0.5, 50)) for a in scatter(200)]
        self.pos_derived = [(a, rng.uniform(0.1, 2)) for a in scatter(200)]
        self.step(0.0)

    # -- one frame --------------------------------------------------------
    def step(self, dt):
        g = self.g
        with self.lock:
            rx, ry, lx, ly = self.stick
        cam = self.cam
        # masters: read back from memory every frame, so an outside write sticks
        if self.model == "hava":
            yaw, pitch = g.rf(cam + self.YAW), g.rf(cam + self.PITCH)
        elif self.model == "hvecva":
            yaw, pitch = math.atan2(g.rf(cam + self.HVEC), g.rf(cam + self.HVEC + 4)), g.rf(cam + self.PITCH)
        else:
            x, y, z = struct.unpack(">3f", g.read(cam + self.DIR, 12))
            n = math.sqrt(x * x + y * y + z * z) or 1.0
            yaw, pitch = math.atan2(z, x), math.asin(max(-1.0, min(1.0, y / n)))
        if not (math.isfinite(yaw) and math.isfinite(pitch)):
            yaw, pitch = 0.0, 0.0
        yaw = wrap_pi(yaw - rx * self.YAW_SPEED * dt)                  # turning right DEcreases yaw
        pitch = min(self.PITCH_MAX, max(self.PITCH_MIN, pitch + ry * self.PITCH_SPEED * dt))
        if self.model == "hava":
            g.wf(cam + self.YAW, yaw)
            g.wf(cam + self.PITCH, pitch)
        elif self.model == "hvecva":
            g.write(cam + self.HVEC, struct.pack(">2f", math.sin(yaw), math.cos(yaw)))
            g.wf(cam + self.PITCH, pitch)
        else:
            g.write(cam + self.DIR, struct.pack(">3f", math.cos(pitch) * math.cos(yaw), math.sin(pitch),
                                                math.cos(pitch) * math.sin(yaw)))

        # derived state the game owns internally and rewrites every frame
        self.yaw_s = wrap_pi(self.yaw_s + wrap_pi(yaw - self.yaw_s) * 0.35)
        self.pitch_s += (pitch - self.pitch_s) * 0.35
        if abs(wrap_pi(yaw - self.yaw_s)) < 1e-6:
            self.yaw_s = yaw
        if abs(pitch - self.pitch_s) < 1e-6:
            self.pitch_s = pitch
        ys, ps = self.yaw_s, self.pitch_s
        g.wf(cam + self.YAW_S, ys)
        g.wf(cam + self.PITCH_S, ps)
        g.wf(cam + self.YAW_DEG, math.degrees(ys))
        d = (math.cos(ps) * math.cos(ys), math.sin(ps), math.cos(ps) * math.sin(ys))
        if self.model != "hvvec":
            g.write(cam + self.DIR, struct.pack(">3f", *d))
        if self.model != "hvecva":
            g.write(cam + self.HVEC, struct.pack(">2f", math.sin(ys), math.cos(ys)))
        if self.model != "hava":
            g.wf(cam + self.YAW, ys)                    # scalar copies are derived in these models
        if self.model == "hvvec":
            g.wf(cam + self.PITCH, ps)

        speed = 3.0 * dt
        self.pos[0] += (lx * math.cos(ys) - ly * math.sin(ys)) * speed
        self.pos[2] += (lx * math.sin(ys) + ly * math.cos(ys)) * speed
        g.write(self.player + 0x30, struct.pack(">3f", *self.pos))
        dist = g.rf(cam + self.DIST)
        cpos = [self.pos[i] - d[i] * dist for i in range(3)]
        g.write(cam + self.POS, struct.pack(">3f", *cpos))
        right = (-math.sin(ys), 0.0, math.cos(ys))
        up = (d[1] * right[2] - d[2] * right[1], d[2] * right[0] - d[0] * right[2], d[0] * right[1] - d[1] * right[0])
        mat = struct.pack(">16f", right[0], up[0], d[0], 0, right[1], up[1], d[1], 0,
                          right[2], up[2], d[2], 0, cpos[0], cpos[1], cpos[2], 1)
        g.write(cam + self.MATRIX, mat)
        g.write(self.STATIC_VIEW, mat)

        for a, k, ph, amp in self.yaw_derived:
            g.wf(a, math.sin(ys * k + ph) * amp)
        for a, k, ph, amp in self.pitch_derived:
            g.wf(a, math.cos(ps * k + ph) * amp)
        for a, k in self.pos_derived:
            g.wf(a, (self.pos[0] + self.pos[2]) * k)
        for a in self.noise:
            g.wf(a, self.rng.uniform(-100, 100))

        self.frame += 1
        g.wf(self.TIME, self.frame / 60.0)
        g.w32(self.TIME + 4, self.frame)
        to_axis = lambda v: max(0, min(255, int(round(128 + 127 * v))))
        buttons = [0] * 24
        buttons[4], buttons[5] = to_axis(rx), to_axis(-ry)
        buttons[6], buttons[7] = to_axis(lx), to_axis(-ly)
        g.write(self.PAD_BUF, struct.pack(">i24H", 24, *buttons))

    def masters(self):
        """(yaw, pitch) as the game currently understands its own master values."""
        g, cam = self.g, self.cam
        if self.model == "hava":
            return g.rf(cam + self.YAW), g.rf(cam + self.PITCH)
        if self.model == "hvecva":
            return math.atan2(g.rf(cam + self.HVEC), g.rf(cam + self.HVEC + 4)), g.rf(cam + self.PITCH)
        x, y, z = struct.unpack(">3f", g.read(cam + self.DIR, 12))
        n = math.sqrt(x * x + y * y + z * z) or 1.0
        return math.atan2(z, x), math.asin(max(-1.0, min(1.0, y / n)))

    def truth(self):
        g = self.g
        hor_master = {"hava": self.YAW, "hvecva": self.HVEC, "hvvec": self.DIR}[self.model]
        yaw_true, pitch_true = self.masters()
        return {"yaw_true": yaw_true, "pitch_true": pitch_true, "cam": self.cam, "cam2": self.cam2, "yaw_addr": self.cam + self.YAW, "model": self.model,
                "hor_master": self.cam + hor_master, "vert_master": self.cam + self.PITCH,
                "pitch_addr": self.cam + self.PITCH, "yaw": g.rf(self.cam + self.YAW),
                "pitch": g.rf(self.cam + self.PITCH), "yaw_s_addr": self.cam + self.YAW_S,
                "yaw_smooth": self.yaw_s, "pitch_smooth": self.pitch_s,
                "game_mgr": self.game_mgr, "cam_mgr": self.cam_mgr, "list_index": self.list_index,
                "static_base": self.G_GAME, "frame": self.frame,
                "chain_yaw": [self.G_GAME, self.OFF_CAMMGR, self.OFF_ACTIVE_CAM, self.YAW],
                "chain_pitch": [self.G_GAME, self.OFF_CAMMGR, self.OFF_ACTIVE_CAM, self.PITCH],
                "pitch_min": self.PITCH_MIN, "pitch_max": self.PITCH_MAX, "pad_buf": self.PAD_BUF,
                "title_id": self.TITLE_ID, "version": self.VERSION, "hash": self.HASH}

    def dump(self, folder):
        os.makedirs(folder, exist_ok=True)
        regions = []
        with open(os.path.join(folder, "guest_memory.bin"), "wb") as f:
            f.truncate(0x1_0000_0000)
            for start, size, py, w, x in sorted(self.g.blocks, key=lambda b: b[0]):
                f.seek(start)
                f.write(py[:])
                regions.append({"start": f"0x{start:08x}", "end_exclusive": f"0x{start + size:08x}",
                                "size": size, "file_offset": f"0x{start:08x}",
                                "raw_page_flags": 0x81 | (2 if w else 0) | (4 if x else 0),
                                "readable": True, "writable": w, "executable": x})
        manifest = {"format": "rpcs3-guest-memory-dump", "version": 1, "title": self.TITLE,
                    "title_id": self.TITLE_ID, "address_space_file": "guest_memory.bin",
                    "address_space_size": "0x100000000", "page_size": 4096,
                    "file_offset_equals_guest_address": True, "sparse_image": True,
                    "regions": regions, "spu_local_stores": []}
        with open(os.path.join(folder, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=1)


# --------------------------------------------------------------------------
# PINE server with RPCS3's semantics
# --------------------------------------------------------------------------
MAX_IPC_SIZE, MAX_IPC_RETURN_SIZE = 650000, 450000


def pine_parse(game: Game, buf: bytes) -> bytes:
    g = game.g
    out = bytearray(5)
    i, n = 0, len(buf)
    rd = {0: 1, 1: 2, 2: 4, 3: 8}
    wr = {4: 1, 5: 2, 6: 4, 7: 8}
    fail = struct.pack("<IB", 5, 0xFF)

    def string(s):
        raw = s.encode() + b"\0"
        if len(out) + 4 + len(raw) >= MAX_IPC_RETURN_SIZE:
            return False
        out.extend(struct.pack("<I", len(raw)) + raw)
        return True

    while i < n:
        op = buf[i]
        i += 1
        if op in rd:
            size = rd[op]
            if i + 4 > n or len(out) + size >= MAX_IPC_RETURN_SIZE:
                return fail
            a = struct.unpack_from("<I", buf, i)[0]
            if not g.mapped(a, size):
                return fail
            out.extend(g.read(a, size)[::-1])          # logical value, little-endian on the wire
            i += 4
        elif op in wr:
            size = wr[op]
            if i + 4 + size > n:
                return fail
            a = struct.unpack_from("<I", buf, i)[0]
            if not g.mapped(a, size) or not g.page_writable(a):
                return fail
            g.write(a, buf[i + 4:i + 4 + size][::-1])
            i += 4 + size
        elif op == 8:
            if not string("RPCS3 0.0.0-fake"):
                return fail
        elif op == 0xB:
            if not string(game.TITLE):
                return fail
        elif op == 0xC:
            if not string(game.TITLE_ID):
                return fail
        elif op == 0xD:
            if not string(game.HASH):
                return fail
        elif op == 0xE:
            if not string(game.VERSION):
                return fail
        elif op == 0xF:
            out.extend(struct.pack("<I", 0))
            i += 4                                      # RPCS3 really does skip 4 bytes here
        else:
            return fail
    struct.pack_into("<IB", out, 0, len(out), 0)
    return bytes(out)


def pine_server(game: Game, listener: socket.socket):
    while True:
        conn, _ = listener.accept()                     # one client at a time, like RPCS3
        try:
            while True:
                head = b""
                while len(head) < 4:
                    chunk = conn.recv(4 - len(head))
                    if not chunk:
                        raise ConnectionError
                    head += chunk
                size = struct.unpack("<I", head)[0]
                if size > MAX_IPC_SIZE or size < 4:
                    raise ConnectionError
                body = bytearray()
                while len(body) < size - 4:
                    chunk = conn.recv(size - 4 - len(body))
                    if not chunk:
                        raise ConnectionError
                    body += chunk
                conn.sendall(pine_parse(game, bytes(body)))
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()


def control_server(game: Game, listener: socket.socket, stop: threading.Event):
    while not stop.is_set():
        conn, _ = listener.accept()
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
                elif parts[0] == "dump":
                    game.dump(parts[1])
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--control-port", type=int, default=28500)
    ap.add_argument("--pine-port", type=int, default=28501, help="TCP port for the PINE server")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--model", choices=("hava", "hvecva", "hvvec"), default="hava")
    args = ap.parse_args()

    libc.prctl(15, b"rpcs3", 0, 0, 0)                  # PR_SET_NAME: tools look for a process named rpcs3
    game = Game(args.seed, args.model)
    stop = threading.Event()

    pine_l = socket.socket()
    pine_l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    pine_l.bind(("127.0.0.1", args.pine_port))
    pine_l.listen(16)
    ctl_l = socket.socket()
    ctl_l.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctl_l.bind(("127.0.0.1", args.control_port))
    ctl_l.listen(4)
    threading.Thread(target=pine_server, args=(game, pine_l), daemon=True).start()
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
