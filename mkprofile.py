#!/usr/bin/env python3
"""mkprofile - turn scan results into something KAMI can use.

    mkprofile.py make  --camfind camfind.json --chains chains.json -o profiles/BCUS98119.json
    mkprofile.py check --profile profiles/BCUS98119.json [--nudge]
    mkprofile.py cs    --profile profiles/BCUS98119.json --class InFamous -o InFamous.cs

make   combines what camfind learned about the values (kind, unit, direction,
       limits) with a pointer chain from ptrscan into a KAMI profile.  KAMI
       loads profiles from its 'profiles' folder at run time and reloads them
       when the file changes, so no rebuild is needed while iterating.
check  resolves the profile against the running game and prints the values;
       with --nudge it also turns the camera right and down the way KAMI would,
       so directions can be confirmed by eye before KAMI is involved.
cs     writes the profile out as a regular KAMI game class (for a pull request).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Dict, List, Optional

from rpcs3mem import add_backend_args, open_backend, parse_int, pine_info
from rpcs3mem.profile import Profile, ProfileError


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_cand(rows: List[dict], addr: int) -> Optional[dict]:
    for c in rows:
        if parse_int(c["addr"]) == addr:
            return c
    return None


def std_wrap(c: dict) -> Optional[List[float]]:
    """A wrap range, but only when the trace actually showed the value wrapping at a standard period."""
    if not c.get("wrap"):
        return None
    lo, hi = c.get("lo", 0.0), c.get("hi", 0.0)
    pi, full = (math.pi, 2 * math.pi) if c["kind"] == "angle_rad" else (180.0, 360.0)
    if lo < -0.9 * pi and hi > 0.9 * pi:
        return [round(-pi, 6), round(pi, 6)]
    if lo > -0.05 * pi and hi > 0.9 * full:
        return [0.0, round(full, 6)]
    return None


def scalar_axis(chain: dict, field: str, c: dict, sign: int, limits: bool) -> dict:
    d = {"base": chain["base"], "offsets": chain["offsets"] + [chain["fields"][field]],
         "unit": "deg" if c["kind"] == "angle_deg" else "rad", "sign": sign}
    if limits:
        if c.get("clamp_lo"):
            d["min"] = round(c["lo"], 5)
        if c.get("clamp_hi"):
            d["max"] = round(c["hi"], 5)
    else:
        wrap = std_wrap(c)
        if wrap:
            d["wrap"] = wrap
    return d


def cmd_make(args) -> int:
    cf, ch = load_json(args.camfind), load_json(args.chains)
    if not ch.get("chains"):
        raise SystemExit("в файле цепочек нет ни одной цепочки")
    chains = ch["chains"]
    if args.index is None:
        index = max(range(len(chains)), key=lambda i: (chains[i].get("hits", 1), chains[i].get("score", 0)))
    else:
        index = args.index
    chain = chains[index]
    targets = {k: parse_int(v) for k, v in ch.get("targets", {}).items()}
    hor = find_cand(cf.get("hor", []), targets.get("hor", -1))
    vert = find_cand(cf.get("vert", []), targets.get("vert", -1))
    if hor is None:
        raise SystemExit("адрес hor из файла цепочек не найден среди кандидатов camfind: "
                         "цепочки и camfind.json должны быть из одного сеанса")

    if hor["kind"] in ("angle_rad", "angle_deg"):
        if vert is None or "vert" not in chain["fields"]:
            raise SystemExit("для камеры HAVA нужен и вертикальный адрес (--target vert=... в ptrscan)")
        cam = {"name": args.camera_name, "type": "HAVA",
               "hor": scalar_axis(chain, "hor", hor, int(hor["sign_right"]), False),
               "vert": scalar_axis(chain, "vert", vert, -int(vert["sign_up"]), True)}
    elif hor["kind"] == "vec2":
        if vert is None or "vert" not in chain["fields"]:
            raise SystemExit("для камеры HVecVA нужен и вертикальный адрес")
        cam = {"name": args.camera_name, "type": "HVecVA",
               "hor": {"base": chain["base"], "offsets": chain["offsets"] + [chain["fields"]["hor"]],
                       "x_offset": f"0x{hor['partner_offset']:X}", "sign": int(hor["sign_right"])},
               "vert": scalar_axis(chain, "vert", vert, -int(vert["sign_up"]), True)}
    elif hor["kind"] == "vec3":
        cam = {"name": args.camera_name, "type": "HVVec",
               "vec": {"base": chain["base"], "offsets": chain["offsets"] + [chain["fields"]["hor"]],
                       "sign_h": int(hor["sign_right"]), "sign_v": -int(hor.get("sign_up", 1))}}
    else:
        raise SystemExit(f"неизвестный тип кандидата {hor['kind']}")

    game = cf.get("game") or ch.get("game") or {}
    exe_hash = args.hash or game.get("hash", "")
    ids = [i.upper() for i in (args.id or ([game["id"]] if game.get("id") else []))]
    versions = args.version or []
    builds: List[dict] = []
    known = load_json(args.known) if args.known and os.path.isfile(args.known) else None
    if known and exe_hash:
        for kb in known.get("builds", []):
            if kb.get("hash", "").lower() == exe_hash.lower():
                builds.append({"name": kb.get("name", ""), "hash": exe_hash, "ids": kb.get("ids", []),
                               "versions": kb.get("versions", [])})
    if not builds:
        if not exe_hash and not ids:
            raise SystemExit("не знаю, что это за игра: задайте --id (и --version) или --hash, "
                             "либо запускайте camfind при включённом IPC и закрытом KAMI")
        builds = [{"name": args.name or (ids[0] if ids else exe_hash), "hash": exe_hash, "ids": ids,
                   "versions": versions}]
    for b in builds:
        b["trace"] = False
        b["cameras"] = [cam]
        b["writes"] = []

    title = args.title or (known or {}).get("title") or game.get("title") or "Unknown game"
    doc = {"schema": 1, "title": title, "builds": []}
    if os.path.isfile(args.output) and not args.replace:
        doc = load_json(args.output)

    def same(a: dict, b: dict) -> bool:
        if a.get("hash") and b.get("hash"):
            return a["hash"].lower() == b["hash"].lower() and a.get("ids") == b.get("ids")
        return a.get("ids") == b.get("ids") and a.get("versions") == b.get("versions")

    for b in builds:
        doc["builds"] = [old for old in doc["builds"] if not same(old, b)] + [b]
    Profile(doc, args.output)                       # validate before writing
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"профиль записан: {args.output}  (сборок: {len(doc['builds'])}, камера {cam['type']})")
    print(f"цепочка #{index}: {chain['base']} " + " ".join(chain["offsets"]) +
          f"  (подтверждений: {chain.get('hits', 1)}, сеансов: {ch.get('sessions', 1)})")
    if ch.get("sessions", 1) < 2:
        print("ВНИМАНИЕ: цепочка не проверена перезапуском игры (ptrscan verify) - может не пережить его.")
    for key in ("hor", "vert"):
        c = hor if key == "hor" else vert
        if c is not None and c.get("sticky") is not True:
            print(f"ВНИМАНИЕ: {key} не прошёл проверку записью (sticky={c.get('sticky')}): игра может "
                  f"перезаписывать это значение, и мышь работать не будет.")
    v_axis = cam.get("vert")
    if v_axis is not None and ("min" not in v_axis or "max" not in v_axis):
        print("Примечание: предел наклона определён не с обеих сторон (в camfind камера не дошла до упора). "
              "Без min/max игра может упереть угол сама; при рывках у упора допишите пределы вручную.")
    print("Скопируйте файл в папку profiles рядом с KAMI.exe; проверка без KAMI: "
          f"python mkprofile.py check --profile {args.output} --nudge")
    return 0


def pick_build(profile: Profile, args, mem):
    info = mem.game_info() or pine_info(getattr(args, "pine", None))
    gid = args.id[0] if getattr(args, "id", None) else info.get("id", "")
    ver = args.version[0] if getattr(args, "version", None) else info.get("version", "")
    exe_hash = getattr(args, "hash", None) or info.get("hash", "")
    build = profile.select(gid, ver, exe_hash)
    if build is None:
        if gid or exe_hash:
            raise SystemExit(f"в профиле нет сборки для игры id={gid!r} version={ver!r} hash={exe_hash!r}")
        build = profile.builds[0]
        print(f"(игра не опознана - IPC выключен или занят KAMI; беру первую сборку профиля: {build.name})")
    return build


def cmd_check(args) -> int:
    try:
        profile = Profile.load(args.profile)
    except (ProfileError, OSError, json.JSONDecodeError) as e:
        print(f"профиль не читается: {e}")
        return 1
    mem = open_backend(args, need_write=args.nudge)
    try:
        build = pick_build(profile, args, mem)
        print(f"сборка: {build.name or '(без имени)'}; камер: {len(build.cameras)}")
        res = build.update(mem, 0.0, 0.0)
        if res is None:
            print("цепочки НЕ разрешаются в текущем состоянии игры (меню, загрузка, другой режим камеры?)")
            return 2
        print(f"камера '{res.get('camera', '')}': hor @0x{res['hor_addr']:08X} = {res['hor']:.5f} рад, "
              f"vert @0x{res['vert_addr']:08X} = {res['vert']:.5f} рад")
        if args.nudge:
            sens = 0.003
            for label, dx, dy in (("ВПРАВО", 120, 0), ("обратно", -120, 0), ("ВНИЗ", 0, 80), ("обратно", 0, -80)):
                print(f"  мышь {label}: камера должна повернуться соответственно ...", flush=True)
                for _ in range(10):                 # spread over ten ticks like real mouse motion
                    build.update(mem, dx / 10 * sens, dy / 10 * sens)
                    time.sleep(0.016)
                time.sleep(args.pause)
                now = build.update(mem, 0.0, 0.0)
                if now:
                    print(f"     hor = {now['hor']:.5f}, vert = {now['vert']:.5f}")
            print("Если камера поворачивалась не туда - поменяйте знак sign у соответствующей оси в профиле.")
    finally:
        mem.close()
    return 0


# --------------------------------------------------------------------------
CS_HEADER = """using KAMI.Core.Cameras;
using KAMI.Core.Utilities;
using System;

namespace KAMI.Core.Games
{{
    // Generated by kami-toolkit/mkprofile.py from {source}
    public class {cls} : Game<{camera}>
    {{
"""


def cs_chain(d: dict) -> str:
    return "DerefChain.CreateDerefChain(ipc, " + ", ".join([d["base"]] + list(d.get("offsets", []))) + ")"


def cs_case(b: dict) -> str:
    ids = b.get("ids", [])
    if not ids:
        raise SystemExit(f"сборка {b.get('name')!r} без ids - для класса C# нужны идентификаторы игры")
    vers = b.get("versions", [])
    guard = ""
    if vers:
        guard = " when " + " || ".join(f'version == "{v}"' for v in vers)
    return "".join(f'                case "{i}"{guard}:\n' for i in ids)


def f32(x: float) -> str:
    return repr(float(x)) + "f"


def cmd_cs(args) -> int:
    doc = load_json(args.profile)
    Profile(doc, args.profile)
    builds = doc["builds"]
    cam0 = builds[0]["cameras"][0]
    ctype = cam0["type"]
    for b in builds:
        if len(b["cameras"]) != 1 or b["cameras"][0]["type"] != ctype or b["cameras"][0].get("when") or b.get("writes"):
            print("предупреждение: условия, несколько камер и принудительные записи в класс не переносятся - "
                  "допишите вручную", file=sys.stderr)
            break
    camera = {"HAVA": "HAVACamera", "HVecVA": "HVecVACamera", "HVVec": "HVVecCamera"}[ctype]
    out = [CS_HEADER.format(source=os.path.basename(args.profile), cls=args.cls, camera=camera)]
    fields = ["m_vec"] if ctype == "HVVec" else ["m_hor", "m_vert"]
    out.append("".join(f"        DerefChain {f};\n" for f in fields))
    out.append(f"\n        public {args.cls}(IntPtr ipc, string id, string version) : base(ipc)\n        {{\n"
               f"            switch (id)\n            {{\n")
    for b in builds:
        cam = b["cameras"][0]
        out.append(cs_case(b))
        if ctype == "HVVec":
            out.append(f"                    m_vec = {cs_chain(cam['vec'])};\n")
        else:
            h, v = cam["hor"], cam["vert"]
            if h["base"] == v["base"] and h["offsets"][:-1] == v["offsets"][:-1] and h["offsets"]:
                prefix = {"base": h["base"], "offsets": h["offsets"][:-1]}
                out.append("                    {\n"
                           f"                        var baseChain = {cs_chain(prefix)};\n"
                           f"                        m_hor = baseChain.Chain({h['offsets'][-1]});\n"
                           f"                        m_vert = baseChain.Chain({v['offsets'][-1]});\n"
                           "                    }\n")
            else:
                out.append(f"                    m_hor = {cs_chain(h)};\n                    m_vert = {cs_chain(v)};\n")
        out.append("                    break;\n")
    out.append("                default:\n"
               f"                    throw new NotImplementedException($\"{{nameof({args.cls})}} ['{{id}}', v'{{version}}'] is not implemented\");\n"
               "            }\n        }\n\n"
               "        public override void UpdateCamera(int diffX, int diffY)\n        {\n")

    def scalar_rw(axis: dict, prop: str, chain: str):
        k = {"rad": None, "deg": "(float)(Math.PI / 180)", "u16": None}[axis.get("unit", "rad")]
        if axis.get("unit") == "u16":
            raise SystemExit("unit u16 в генераторе класса не поддержан - см. MetalGearSolid4.cs как образец")
        rd = f"IPCUtils.ReadFloat(m_ipc, (uint){chain}.Value)"
        read = f"                m_camera.{prop} = {rd}{' * ' + k if k else ''};\n"
        write = f"                IPCUtils.WriteFloat(m_ipc, (uint){chain}.Value, m_camera.{prop}{' / ' + k if k else ''});\n"
        return read, write, (" * " + k if k else "")

    def sgn(axis: dict, var: str) -> str:
        s = float(axis.get("sign", 1)) * float(axis.get("scale", 1.0))
        return (f"{var}" if s == 1 else f"-{var}" if s == -1 else f"{var} * {f32(s)}") + " * SensModifier"

    cam = cam0
    if ctype == "HAVA":
        hr, hw, _ = scalar_rw(cam["hor"], "Hor", "m_hor")
        vr, vw, vk = scalar_rw(cam["vert"], "Vert", "m_vert")
        out.append("            if (DerefChain.VerifyChains(m_hor, m_vert))\n            {\n" + hr + vr)
        out.append(f"                m_camera.Update({sgn(cam['hor'], 'diffX')}, {sgn(cam['vert'], 'diffY')});\n")
        v = cam["vert"]
        if v.get("min") is not None and v.get("max") is not None:
            out.append(f"                m_camera.Vert = Math.Clamp(m_camera.Vert, {f32(v['min'])}{vk}, {f32(v['max'])}{vk});\n")
        elif v.get("min") is not None:
            out.append(f"                m_camera.Vert = Math.Max(m_camera.Vert, {f32(v['min'])}{vk});\n")
        elif v.get("max") is not None:
            out.append(f"                m_camera.Vert = Math.Min(m_camera.Vert, {f32(v['max'])}{vk});\n")
        out.append(hw + vw + "            }\n")
    elif ctype == "HVecVA":
        xo = cam["hor"].get("x_offset", "0x4")
        vr, vw, vk = scalar_rw(cam["vert"], "Vert", "m_vert")
        out.append("            if (DerefChain.VerifyChains(m_hor, m_vert))\n            {\n"
                   "                m_camera.HorY = IPCUtils.ReadFloat(m_ipc, (uint)m_hor.Value);\n"
                   f"                m_camera.HorX = IPCUtils.ReadFloat(m_ipc, (uint)(m_hor.Value + {xo}));\n" + vr +
                   f"                m_camera.Update({sgn(cam['hor'], 'diffX')}, {sgn(cam['vert'], 'diffY')});\n")
        v = cam["vert"]
        if v.get("min") is not None and v.get("max") is not None:
            out.append(f"                m_camera.Vert = Math.Clamp(m_camera.Vert, {f32(v['min'])}{vk}, {f32(v['max'])}{vk});\n")
        out.append("                IPCUtils.WriteFloat(m_ipc, (uint)m_hor.Value, m_camera.HorY);\n"
                   f"                IPCUtils.WriteFloat(m_ipc, (uint)(m_hor.Value + {xo}), m_camera.HorX);\n" + vw +
                   "            }\n")
    else:
        vec = cam["vec"]
        sh = {"sign": vec.get("sign_h", 1)}
        sv = {"sign": vec.get("sign_v", 1)}
        out.append("            if (m_vec.Verify())\n            {\n"
                   "                m_camera.X = IPCUtils.ReadFloat(m_ipc, (uint)m_vec.Value);\n"
                   "                m_camera.Y = IPCUtils.ReadFloat(m_ipc, (uint)(m_vec.Value + 4));\n"
                   "                m_camera.Z = IPCUtils.ReadFloat(m_ipc, (uint)(m_vec.Value + 8));\n"
                   f"                m_camera.Update({sgn(sh, 'diffX')}, {sgn(sv, 'diffY')});\n"
                   "                IPCUtils.WriteFloat(m_ipc, (uint)m_vec.Value, m_camera.X);\n"
                   "                IPCUtils.WriteFloat(m_ipc, (uint)(m_vec.Value + 4), m_camera.Y);\n"
                   "                IPCUtils.WriteFloat(m_ipc, (uint)(m_vec.Value + 8), m_camera.Z);\n"
                   "            }\n")
    out.append("        }\n    }\n}\n")
    text = "".join(out)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"класс записан: {args.output}")
    else:
        print(text)
    print("\n// Добавьте в KAMI.Core/GameManager.cs, в switch (id):", file=sys.stderr)
    for b in builds:
        for i in b.get("ids", []):
            print(f'                case "{i}":', file=sys.stderr)
    print(f"                    return new {args.cls}(ipc, id, version);", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("make", help="build a KAMI profile from camfind + ptrscan results")
    m.add_argument("--camfind", required=True)
    m.add_argument("--chains", required=True)
    m.add_argument("--index", type=int, help="which chain to use (default: most confirmed, best score)")
    m.add_argument("--title")
    m.add_argument("--name", help="build name")
    m.add_argument("--camera-name", default="default")
    m.add_argument("--id", action="append", help="game id, e.g. BCUS98119 (repeatable)")
    m.add_argument("--version", action="append", help="game version, e.g. 01.00 (repeatable)")
    m.add_argument("--hash", help="executable hash as KAMI shows it (PPU-...)")
    m.add_argument("--known", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "games", "infamous.json"),
                   help="table of known builds used to add sibling game IDs that share the hash")
    m.add_argument("--replace", action="store_true", help="overwrite the output instead of merging into it")
    m.add_argument("-o", "--output", required=True)
    m.set_defaults(func=cmd_make)

    c = sub.add_parser("check", help="resolve a profile against the running game")
    add_backend_args(c)
    c.add_argument("--profile", required=True)
    c.add_argument("--id", action="append")
    c.add_argument("--version", action="append")
    c.add_argument("--hash")
    c.add_argument("--nudge", action="store_true", help="move the camera right and down like KAMI would")
    c.add_argument("--pause", type=float, default=1.0)
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("cs", help="emit a KAMI game class from a profile")
    s.add_argument("--profile", required=True)
    s.add_argument("--class", dest="cls", required=True)
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_cs)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
