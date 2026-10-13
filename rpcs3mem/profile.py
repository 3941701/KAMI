"""KAMI camera profiles: the JSON format and a reference implementation.

A profile describes, as data, what a hand-written KAMI game class does: which
pointer chains lead to the camera values and how to apply a mouse delta to
them.  KAMI.Core/Games/ProfileGame.cs implements exactly the logic in this
file; keeping a Python twin lets a profile be checked against a running game
(mkprofile.py check) before KAMI is rebuilt, and lets the tests drive the
simulator the way KAMI would.

    {
      "schema": 1,
      "title": "inFAMOUS",
      "builds": [{
        "name": "US disc",
        "hash": "PPU-...",            optional; when present it must equal RPCS3's executable hash
        "ids": ["BCUS98119"],         used when there is no hash
        "versions": [],               empty = any
        "trace": false,               true: KAMI appends kami_trace.csv
        "cameras": [{
          "name": "third person",
          "type": "HAVA",             HAVA | HVecVA | HVVec  (KAMI's three camera models)
          "when": {chain..., "type": "u32", "op": "==", "value": 3},     optional
          "hor":  {"base": "0x..", "offsets": ["0x5C", "0x118", "0x1A8"],
                   "unit": "rad", "sign": -1, "scale": 1.0, "wrap": [-3.14159, 3.14159]},
          "vert": {"base": "0x..", "offsets": [...], "unit": "rad", "sign": 1, "min": -1.0, "max": 1.2}
        }],
        "writes": [{"base": "0x..", "offsets": [], "type": "float", "value": 0}]
      }]
    }

Chains use KAMI's DerefChain semantics: value0 = base, value[i] =
read32(value[i-1]) + offsets[i]; an empty offsets list is a plain static address.

sign: what the stored quantity does for a positive mouse delta (mouse right for
hor, mouse DOWN for vert - KAMI's diffY grows downwards).
unit: rad, deg, or u16 (65536 per turn, stored as a 16-bit integer).
min/max/wrap are in the stored unit.

  HAVA    hor and vert are two scalars.
  HVecVA  hor is a sin/cos pair: HorY at the chain address, HorX at +x_offset
          (angle = atan2(HorY, HorX)); vert is a scalar.
  HVVec   "vec" is a direction vector X,Y,Z at +0,+4,+8 (offsets configurable):
          hor angle = atan2(Z, X), vert angle = asin(Y / length).
"""
from __future__ import annotations

import json
import math
import struct
from typing import List, Optional

from .backend import resolve_chain
from .common import MemoryError_, parse_int

TWO_PI = 2.0 * math.pi
UNIT_TO_RAD = {"rad": 1.0, "deg": math.pi / 180.0, "u16": TWO_PI / 65536.0}
VALUE_FMT = {"float": (">f", 4), "u8": (">B", 1), "u16": (">H", 2), "u32": (">I", 4)}


class ProfileError(ValueError):
    pass


class Chain:
    def __init__(self, d: dict, what: str):
        try:
            self.base = parse_int(d["base"])
            self.offsets = [parse_int(o) for o in d.get("offsets", [])]
        except (KeyError, ValueError, TypeError) as e:
            raise ProfileError(f"{what}: bad chain ({e})") from e

    def resolve(self, mem) -> Optional[int]:
        return resolve_chain(mem, self.base, self.offsets) if self.offsets else self.base

    def to_json(self) -> dict:
        return {"base": f"0x{self.base:08X}", "offsets": [f"0x{o:X}" for o in self.offsets]}


class Axis(Chain):
    """A scalar angle: where it is, its unit, direction and limits."""

    def __init__(self, d: dict, what: str):
        super().__init__(d, what)
        self.unit = d.get("unit", "rad")
        if self.unit not in UNIT_TO_RAD:
            raise ProfileError(f"{what}: unit must be rad, deg or u16")
        self.k = UNIT_TO_RAD[self.unit]
        self.sign = float(d.get("sign", 1))
        self.scale = float(d.get("scale", 1.0))
        self.min = float(d["min"]) * self.k if d.get("min") is not None else None
        self.max = float(d["max"]) * self.k if d.get("max") is not None else None
        wrap = d.get("wrap")
        self.wrap = (float(wrap[0]) * self.k, float(wrap[1]) * self.k) if wrap else None

    def read(self, mem, addr: int) -> float:
        if self.unit == "u16":
            return struct.unpack(">H", mem.read(addr, 2))[0] * self.k
        return struct.unpack(">f", mem.read(addr, 4))[0] * self.k

    def write(self, mem, addr: int, rad: float) -> None:
        if self.unit == "u16":
            mem.write(addr, struct.pack(">H", int(round(rad / self.k)) & 0xFFFF))
        else:
            mem.write(addr, struct.pack(">f", rad / self.k))

    def apply(self, rad: float, delta: float) -> float:
        rad += delta * self.sign * self.scale
        if self.min is not None:
            rad = max(self.min, rad)
        if self.max is not None:
            rad = min(self.max, rad)
        if self.wrap:
            lo, hi = self.wrap
            span = hi - lo
            if span > 0:
                rad = lo + (rad - lo) % span
        return rad


class Condition(Chain):
    OPS = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, "&": lambda a, b: (int(a) & int(b)) != 0,
           "<": lambda a, b: a < b, ">": lambda a, b: a > b}

    def __init__(self, d: dict, what: str):
        super().__init__(d, what)
        self.type = d.get("type", "u32")
        self.op = d.get("op", "==")
        if self.type not in VALUE_FMT or self.op not in self.OPS:
            raise ProfileError(f"{what}: bad condition type/op")
        self.value = float(d["value"]) if self.type == "float" else parse_int(d["value"])

    def test(self, mem) -> bool:
        addr = self.resolve(mem)
        if addr is None:
            return False
        fmt, size = VALUE_FMT[self.type]
        try:
            cur = struct.unpack(fmt, mem.read(addr, size))[0]
        except MemoryError_:
            return False
        return self.OPS[self.op](cur, self.value)


class Camera:
    def __init__(self, d: dict, what: str):
        self.name = d.get("name", "")
        self.type = d.get("type", "HAVA")
        self.when = Condition(d["when"], what + ".when") if d.get("when") else None
        if self.type == "HAVA":
            self.hor = Axis(d["hor"], what + ".hor")
            self.vert = Axis(d["vert"], what + ".vert")
        elif self.type == "HVecVA":
            self.hor = Axis(d["hor"], what + ".hor")
            self.x_offset = parse_int(d["hor"].get("x_offset", 4))
            self.vert = Axis(d["vert"], what + ".vert")
        elif self.type == "HVVec":
            v = d["vec"]
            self.vec = Chain(v, what + ".vec")
            self.x_off = parse_int(v.get("x_offset", 0))
            self.y_off = parse_int(v.get("y_offset", 4))
            self.z_off = parse_int(v.get("z_offset", 8))
            self.sign_h = float(v.get("sign_h", 1))
            self.sign_v = float(v.get("sign_v", 1))
            self.vmin = float(v.get("vert_min", -math.pi / 2 + 0.01))
            self.vmax = float(v.get("vert_max", math.pi / 2 - 0.01))
        else:
            raise ProfileError(f"{what}: unknown camera type {self.type!r}")

    # dx, dy: mouse delta already multiplied by the sensitivity (radians)
    def update(self, mem, dx: float, dy: float) -> Optional[dict]:
        """Apply a delta.  Returns what was written, or None when a chain is not resolvable."""
        rf = lambda a: struct.unpack(">f", mem.read(a, 4))[0]
        wf = lambda a, v: mem.write(a, struct.pack(">f", v))
        try:
            if self.type == "HAVA":
                ha, va = self.hor.resolve(mem), self.vert.resolve(mem)
                if ha is None or va is None:
                    return None
                h, v = self.hor.read(mem, ha), self.vert.read(mem, va)
                if not (math.isfinite(h) and math.isfinite(v)):
                    return None
                if dx:
                    h = self.hor.apply(h, dx)
                    self.hor.write(mem, ha, h)
                if dy:
                    v = self.vert.apply(v, dy)
                    self.vert.write(mem, va, v)
                return {"hor_addr": ha, "vert_addr": va, "hor": h, "vert": v}
            if self.type == "HVecVA":
                ha, va = self.hor.resolve(mem), self.vert.resolve(mem)
                if ha is None or va is None:
                    return None
                y, x = rf(ha), rf(ha + self.x_offset)
                v = self.vert.read(mem, va)
                r = math.hypot(x, y)
                if not (math.isfinite(r) and math.isfinite(v)) or r < 1e-6:
                    return None
                ang = math.atan2(y, x)
                if dx:
                    ang += dx * self.hor.sign * self.hor.scale
                    wf(ha, math.sin(ang) * r)
                    wf(ha + self.x_offset, math.cos(ang) * r)
                if dy:
                    v = self.vert.apply(v, dy)
                    self.vert.write(mem, va, v)
                return {"hor_addr": ha, "vert_addr": va, "hor": ang, "vert": v}
            a = self.vec.resolve(mem)
            if a is None:
                return None
            x, y, z = rf(a + self.x_off), rf(a + self.y_off), rf(a + self.z_off)
            scale = math.sqrt(x * x + y * y + z * z)
            if not math.isfinite(scale) or scale < 1e-6:
                return None
            h = math.atan2(z, x)
            v = math.asin(max(-1.0, min(1.0, y / scale)))
            if dx or dy:
                h += dx * self.sign_h
                v = max(self.vmin, min(self.vmax, v + dy * self.sign_v))
                wf(a + self.x_off, math.cos(h) * math.cos(v) * scale)
                wf(a + self.z_off, math.sin(h) * math.cos(v) * scale)
                wf(a + self.y_off, math.sin(v) * scale)
            return {"hor_addr": a, "vert_addr": a + self.y_off, "hor": h, "vert": v}
        except MemoryError_:
            return None


class Write(Chain):
    def __init__(self, d: dict, what: str):
        super().__init__(d, what)
        self.type = d.get("type", "float")
        if self.type not in VALUE_FMT:
            raise ProfileError(f"{what}: bad type")
        self.value = float(d["value"]) if self.type == "float" else parse_int(d["value"])
        self.when = Condition(d["when"], what + ".when") if d.get("when") else None

    def apply(self, mem) -> None:
        if self.when and not self.when.test(mem):
            return
        addr = self.resolve(mem)
        if addr is None:
            return
        try:
            mem.write(addr, struct.pack(VALUE_FMT[self.type][0], self.value))
        except MemoryError_:
            pass


class Build:
    def __init__(self, d: dict, what: str):
        self.name = d.get("name", "")
        self.hash = (d.get("hash") or "").strip()
        self.ids = [s.upper() for s in d.get("ids", [])]
        self.versions = list(d.get("versions", []))
        self.trace = bool(d.get("trace", False))
        self.cameras = [Camera(c, f"{what}.cameras[{i}]") for i, c in enumerate(d.get("cameras", []))]
        self.writes = [Write(w, f"{what}.writes[{i}]") for i, w in enumerate(d.get("writes", []))]
        if not self.cameras:
            raise ProfileError(f"{what}: no cameras")
        if not self.hash and not self.ids:
            raise ProfileError(f"{what}: needs a hash or at least one id")

    def matches(self, game_id: str, version: str, exe_hash: str) -> bool:
        if self.hash:
            return self.hash.lower() == (exe_hash or "").lower()
        return game_id.upper() in self.ids and (not self.versions or version in self.versions)

    def update(self, mem, dx: float, dy: float) -> Optional[dict]:
        for w in self.writes:
            w.apply(mem)
        for cam in self.cameras:
            if cam.when is None or cam.when.test(mem):
                res = cam.update(mem, dx, dy)
                if res is not None:
                    res["camera"] = cam.name
                return res
        return None


class Profile:
    def __init__(self, doc: dict, source: str = "<profile>"):
        if doc.get("schema", 1) != 1:
            raise ProfileError(f"{source}: unsupported schema {doc.get('schema')}")
        self.title = doc.get("title", "")
        self.builds: List[Build] = [Build(b, f"{source}: builds[{i}]") for i, b in enumerate(doc.get("builds", []))]
        if not self.builds:
            raise ProfileError(f"{source}: no builds")

    @classmethod
    def load(cls, path: str) -> "Profile":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), path)

    def select(self, game_id: str, version: str, exe_hash: str) -> Optional[Build]:
        for b in self.builds:
            if b.matches(game_id, version, exe_hash):
                return b
        return None
