#!/usr/bin/env python3
"""ptrscan - find pointer paths from the executable's static data to a heap address.

The camera object is allocated at run time, so its address changes between
sessions.  KAMI reaches such objects through a DerefChain: start at a fixed
address in the game's own data segment and follow pointers.  This tool finds
those chains and prints them in KAMI's form.

    ptrscan.py find   --target hor=0x30251XXX --target vert=0x30251YYY -o chains.json
    (restart the game / load another area, locate the values again)
    ptrscan.py verify --chains chains.json --target hor=0x30A40XXX -o chains.json
    ptrscan.py show   --chains chains.json --cs
    ptrscan.py show   --chains chains.json --rpcs3-cheats      (watch the chains in RPCS3's own Cheat Manager)

`verify` without --target resolves every chain and reports where they agree
(consensus), which is usually enough to see whether the chains survived.

Chain semantics are KAMI's (Utilities/DerefChain.cs):
    value[0] = base;  value[i] = read32(value[i-1]) + offset[i]
so  CreateDerefChain(ipc, base, o1, o2).Chain(field)  ends at the field itself.

Works on a live game (process or PINE backend) and on an RPCS3 guest memory
dump (--dump DIR), which is the most convenient source for a deep scan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from typing import Dict, List, Tuple

import numpy as np

from rpcs3mem import add_backend_args, open_backend, parse_int, parse_range, pine_info, resolve_chain
from rpcs3mem.backend import read_f32
from rpcs3mem.common import MemoryError_
from rpcs3mem.scan import Snapshot, scan_regions, static_ranges


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_target(text: str) -> Tuple[str, int]:
    if "=" in text:
        name, value = text.split("=", 1)
        return name.strip(), parse_int(value)
    return "", parse_int(text)


def named_targets(items: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for i, item in enumerate(items):
        name, addr = parse_target(item)
        out[name or ("hor", "vert", "f2", "f3", "f4")[min(i, 4)]] = addr
    return out


def in_ranges(values: np.ndarray, ranges: List[Tuple[int, int]]) -> np.ndarray:
    """Boolean mask: value falls inside one of the sorted, disjoint [lo, hi) ranges."""
    if not ranges:
        return np.zeros(len(values), dtype=bool)
    los = np.array([r[0] for r in ranges], dtype=np.int64)
    his = np.array([r[1] for r in ranges], dtype=np.int64)
    v = values.astype(np.int64)
    idx = np.searchsorted(los, v, side="right") - 1
    ok = idx >= 0
    idx = np.clip(idx, 0, len(his) - 1)
    return ok & (v < his[idx])


class PointerMap:
    """Every aligned word in memory whose value points into mapped memory, sorted by value."""

    def __init__(self, mem, lo: int, hi: int):
        t0 = time.time()
        regions = scan_regions(mem, lo, hi)
        if not regions:
            raise SystemExit("no readable memory in range")
        snap = Snapshot(mem, regions)
        spans = sorted((r.start, r.end) for r in regions)
        addr_parts, val_parts = [], []
        for i, r in enumerate(regions):
            w = snap.words(i)
            coarse = np.flatnonzero((w >= lo) & (w < hi))
            vals = w[coarse].astype(np.uint32)
            keep = in_ranges(vals, spans)
            addr_parts.append((r.start + 4 * coarse[keep]).astype(np.uint32))
            val_parts.append(vals[keep])
        addrs = np.concatenate(addr_parts)
        vals = np.concatenate(val_parts)
        order = np.argsort(vals, kind="stable")
        self.vals = vals[order]
        self.addrs = addrs[order]
        self.bytes = snap.nbytes
        log(f"карта указателей: {len(self.vals)} указателей в {self.bytes / 1048576:.0f} МиБ "
            f"за {time.time() - t0:.1f} c")

    def pointing_into(self, nodes: np.ndarray, max_off: int, align: int):
        """All (node_index, pointer_location, offset) with 0 <= node - value <= max_off."""
        n64 = nodes.astype(np.int64)
        lo = np.searchsorted(self.vals, np.maximum(n64 - max_off, 0).astype(np.uint32), side="left")
        hi = np.searchsorted(self.vals, nodes, side="right")
        counts = (hi - lo).astype(np.int64)
        total = int(counts.sum())
        if total == 0:
            z = np.zeros(0, dtype=np.int64)
            return z, z.astype(np.uint32), z
        node_idx = np.repeat(np.arange(len(nodes), dtype=np.int64), counts)
        starts = np.repeat(lo.astype(np.int64), counts)
        within = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
        ptr_idx = starts + within
        offs = n64[node_idx] - self.vals[ptr_idx].astype(np.int64)
        ok = offs % align == 0
        return node_idx[ok], self.addrs[ptr_idx[ok]], offs[ok]


def find_chains(pm: PointerMap, target: int, statics: List[Tuple[int, int]], max_off: int,
                max_depth: int, align: int, frontier_limit: int, max_chains: int) -> List[dict]:
    chains: List[dict] = []
    seen = np.array([target], dtype=np.uint32)
    # per level: node addresses, index of the parent node in the previous level, offset to the parent
    levels = [(np.array([target], dtype=np.uint32), np.array([-1], dtype=np.int64), np.array([0], dtype=np.int64))]
    for depth in range(1, max_depth + 1):
        nodes = levels[-1][0]
        node_idx, locs, offs = pm.pointing_into(nodes, max_off, align)
        if len(locs) == 0:
            break
        is_static = in_ranges(locs, statics)

        def path(parent: int, first_off: int) -> List[int]:
            offsets, lvl, idx = [first_off], len(levels) - 1, parent
            while lvl > 0:
                offsets.append(int(levels[lvl][2][idx]))
                idx = int(levels[lvl][1][idx])
                lvl -= 1
            return offsets

        for k in np.flatnonzero(is_static):
            offsets = path(int(node_idx[k]), int(offs[k]))
            chains.append({"base": int(locs[k]), "offsets": offsets, "depth": depth})
            if len(chains) >= max_chains:
                break
        log(f"  глубина {depth}: узлов {len(nodes)}, указателей на них {len(locs)}, "
            f"из статики {int(is_static.sum())}, всего цепочек {len(chains)}")
        if len(chains) >= max_chains or depth == max_depth:
            break
        # next frontier: heap locations not visited yet; one (nearest) parent each
        dyn = ~is_static
        locs_d, node_d, offs_d = locs[dyn], node_idx[dyn], offs[dyn]
        fresh = ~np.isin(locs_d, seen)
        locs_d, node_d, offs_d = locs_d[fresh], node_d[fresh], offs_d[fresh]
        if len(locs_d) == 0:
            break
        order = np.lexsort((locs_d, offs_d))            # small offsets first
        locs_d, node_d, offs_d = locs_d[order], node_d[order], offs_d[order]
        _, first = np.unique(locs_d, return_index=True)
        first.sort()
        if len(first) > frontier_limit:
            first = first[:frontier_limit]
        levels.append((locs_d[first], node_d[first], offs_d[first]))
        seen = np.concatenate((seen, locs_d[first]))
    return chains


def rank(chains: List[dict]) -> None:
    for c in chains:
        offs = c["offsets"]
        # short chains with small offsets from a low static address tend to be the real ones
        c["score"] = round(10.0 - 1.5 * (c["depth"] - 1) - sum(o > 0x400 for o in offs) * 0.75
                           - max(offs) / 0x1000, 2)
    chains.sort(key=lambda c: (-c["score"], c["base"], c["offsets"]))


def chain_to_json(c: dict) -> dict:
    out = {"base": f"0x{c['base']:08X}", "offsets": [f"0x{o:X}" for o in c["offsets"][:-1]],
           "fields": {k: f"0x{v:X}" for k, v in c["fields"].items()},
           "depth": c["depth"], "score": c.get("score", 0)}
    if "hits" in c:
        out["hits"] = c["hits"]
    return out


def chain_from_json(d: dict) -> dict:
    return {"base": parse_int(d["base"]), "prefix": [parse_int(o) for o in d["offsets"]],
            "fields": {k: parse_int(v) for k, v in d["fields"].items()},
            "depth": d.get("depth", len(d["offsets"]) + 1), "score": d.get("score", 0),
            "hits": d.get("hits", 1)}


def cs_snippet(d: dict, cls: str = "") -> str:
    base, offs, fields = d["base"], d["offsets"], d["fields"]
    args = ", ".join([base] + offs)
    lines = [f"var baseChain = DerefChain.CreateDerefChain(ipc, {args});"]
    for name, off in fields.items():
        lines.append(f"m_{name} = baseChain.Chain({off});")
    return "\n".join(lines)


def rpcs3_cheat_script(offsets: List[int]) -> str:
    """The chain as an RPCS3 Cheat Manager "redirection script".

    In such a script `[x]` reads the 32-bit value at x and numbers are decimal
    (rpcs3qt/cheat_manager.cpp, resolve_script).  The chain base is written out as a number
    rather than `$` (the cheat's own offset) because RPCS3 keeps one cheat per game and offset,
    and several fields share a base.
    """
    script = str(offsets[0])
    for off in offsets[1:]:
        script = f"[{script}]+{off}"
    return script


def rpcs3_cheat_lines(doc: dict, serial: str, top: int) -> str:
    """Text for Manage > Cheats > right-click > Import Cheats (it reads the clipboard)."""
    lines = []
    for i, d in enumerate(doc["chains"][:top], 1):
        base = parse_int(d["base"])
        prefix = [parse_int(o) for o in d["offsets"]]
        for name, off in d["fields"].items():
            script = rpcs3_cheat_script([base] + prefix + [parse_int(off)])
            # type 8 = "Float 32 bits".  The offset column only has to be unique per entry (the
            # script does not use it), so a running number is used.
            lines.append(f"{serial}@@@KAMI chain {i} {name}@@@8@@@{len(lines) + 1}@@@{script}@@@")
    return "^^^".join(lines)


# --------------------------------------------------------------------------
def cmd_find(args) -> int:
    targets = named_targets(args.target)
    names = list(targets)
    primary = targets[names[0]]
    for name in names[1:]:
        if not (0 <= abs(targets[name] - primary) < 0x1000):
            log(f"предупреждение: {name}=0x{targets[name]:08X} далеко от {names[0]} - похоже, это разные "
                f"объекты; для него запустите отдельный поиск")
    mem = open_backend(args)
    try:
        info = mem.game_info() or pine_info(args.pine)
        statics = [parse_range(s) for s in args.static] if args.static else static_ranges(mem)
        log("статические диапазоны: " + ", ".join(f"{a:08X}-{b:08X}" for a, b in statics))
        lo, hi = args.range
        pm = PointerMap(mem, lo, hi)
        chains = find_chains(pm, primary, sorted(statics), args.max_offset, args.max_depth,
                             args.align, args.frontier, args.max_chains)
    finally:
        mem.close()
    out = []
    for c in chains:
        last = c["offsets"][-1]
        fields = {}
        for name in names:
            off = last + (targets[name] - primary)
            if off >= 0:
                fields[name] = off
        if len(fields) == len(names) or len(names) == 1:
            c["fields"] = fields
            out.append(c)
    rank(out)
    doc = {"tool": "ptrscan", "game": info, "targets": {k: f"0x{v:08X}" for k, v in targets.items()},
           "static_ranges": [[f"0x{a:08X}", f"0x{b:08X}"] for a, b in statics],
           "params": {"max_offset": args.max_offset, "max_depth": args.max_depth},
           "sessions": 1, "chains": [chain_to_json(c) for c in out]}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"найдено цепочек: {len(out)}; записано в {args.output}")
    for d in doc["chains"][:args.top]:
        print(f"  {d['base']} " + " ".join(d["offsets"]) + " | " +
              ", ".join(f"{k}={v}" for k, v in d["fields"].items()) + f"   (балл {d['score']})")
    if not out:
        print("ничего не найдено: увеличьте --max-depth / --max-offset или задайте --static вручную")
        return 2
    print("\nТеперь перезапустите игру (или загрузите другую локацию), найдите значения заново и\n"
          "выполните: python ptrscan.py verify --chains " + args.output + " --target hor=0x...")
    return 0


def cmd_verify(args) -> int:
    with open(args.chains, "r", encoding="utf-8") as f:
        doc = json.load(f)
    chains = [chain_from_json(d) for d in doc["chains"]]
    targets = named_targets(args.target) if args.target else {}
    mem = open_backend(args)
    try:
        resolved = []
        for c in chains:
            res = {}
            for name, off in c["fields"].items():
                res[name] = resolve_chain(mem, c["base"], c["prefix"] + [off])
            resolved.append(res)
        first = next(iter(chains[0]["fields"])) if chains else "hor"
        if targets:
            keep = [all(res.get(n) == a for n, a in targets.items()) for res in resolved]
        else:
            votes = Counter(res.get(first) for res in resolved if res.get(first) is not None)
            if not votes:
                print("ни одна цепочка не разрешилась - игра в другом состоянии или цепочки негодные")
                return 2
            print("куда сходятся цепочки (адрес поля, сколько цепочек, текущее значение):")
            ranked = votes.most_common(8)
            for addr, n in ranked:
                try:
                    val = f"{read_f32(mem, addr):.5f}"
                except MemoryError_:
                    val = "не читается"
                print(f"   0x{addr:08X}  {n:5d}  {val}")
            pick = ranked[min(args.pick, len(ranked) - 1)][0]
            print(f"оставляю цепочки, ведущие к 0x{pick:08X} (другую группу выбирает --pick N)")
            keep = [res.get(first) == pick for res in resolved]
    finally:
        mem.close()
    survivors = []
    for c, k in zip(chains, keep):
        if k:
            c["hits"] = c.get("hits", 1) + 1
            survivors.append(c)
    doc["sessions"] = doc.get("sessions", 1) + 1
    out_chains = []
    for c in survivors:
        d = {"base": f"0x{c['base']:08X}", "offsets": [f"0x{o:X}" for o in c["prefix"]],
             "fields": {k: f"0x{v:X}" for k, v in c["fields"].items()},
             "depth": c["depth"], "score": c["score"], "hits": c["hits"]}
        out_chains.append(d)
    doc["chains"] = out_chains
    out = args.output or args.chains
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"выжило {len(survivors)} из {len(chains)} цепочек (сеансов проверки: {doc['sessions']}); "
          f"записано в {out}")
    for d in out_chains[:args.top]:
        print(f"  {d['base']} " + " ".join(d["offsets"]) + " | " +
              ", ".join(f"{k}={v}" for k, v in d["fields"].items()))
    return 0 if survivors else 2


def cmd_show(args) -> int:
    with open(args.chains, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if args.rpcs3_cheats:
        serial = args.rpcs3_cheats if args.rpcs3_cheats != "auto" else (doc.get("game") or {}).get("id", "")
        if not serial:
            print("в файле нет идентификатора игры: укажите его, например --rpcs3-cheats BCUS98119")
            return 2
        print(rpcs3_cheat_lines(doc, serial, args.top))
        print("\n(скопируйте строку выше в буфер обмена; в RPCS3: Manage > Cheats, правый щелчок по таблице > "
              "Import Cheats. Выберите строку - поле Current Value покажет значение по цепочке.)", file=sys.stderr)
        return 0
    print(f"цепочек: {len(doc['chains'])}, сеансов проверки: {doc.get('sessions', 1)}")
    for d in doc["chains"][:args.top]:
        print(f"\n# база {d['base']}, глубина {d['depth']}, балл {d.get('score')}, совпадений {d.get('hits', 1)}")
        if args.cs:
            print(cs_snippet(d))
        else:
            print("  " + " -> ".join([f"[{d['base']}]"] + [f"+{o}" for o in d["offsets"]]) +
                  "   поля: " + ", ".join(f"{k}=+{v}" for k, v in d["fields"].items()))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="scan memory for pointer chains to a target address")
    add_backend_args(f)
    f.add_argument("--target", action="append", required=True, metavar="[NAME=]ADDR",
                   help="field address; first one is the primary, others must be in the same object")
    f.add_argument("--max-offset", type=parse_int, default=0x800)
    f.add_argument("--max-depth", type=int, default=5)
    f.add_argument("--align", type=int, default=4)
    f.add_argument("--range", type=parse_range, default=(0x10000, 0xC0000000), metavar="LO-HI")
    f.add_argument("--static", action="append", metavar="LO-HI",
                   help="where chains may start (default: writable segments of the main ELF)")
    f.add_argument("--frontier", type=int, default=200000, help="max nodes kept per level")
    f.add_argument("--max-chains", type=int, default=20000)
    f.add_argument("--top", type=int, default=15)
    f.add_argument("-o", "--output", default="chains.json")
    f.set_defaults(func=cmd_find)

    v = sub.add_parser("verify", help="re-check saved chains in a new session")
    add_backend_args(v)
    v.add_argument("--chains", required=True)
    v.add_argument("--target", action="append", metavar="[NAME=]ADDR",
                   help="where the field is now; omit to use consensus between chains")
    v.add_argument("--pick", type=int, default=0, help="consensus group to keep (0 = largest)")
    v.add_argument("--top", type=int, default=15)
    v.add_argument("-o", "--output")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("show", help="print saved chains")
    s.add_argument("--chains", required=True)
    s.add_argument("--cs", action="store_true", help="print as KAMI C# DerefChain code")
    s.add_argument("--rpcs3-cheats", nargs="?", const="auto", metavar="SERIAL",
                   help="print the chains as RPCS3 Cheat Manager entries (to watch them inside RPCS3)")
    s.add_argument("--top", type=int, default=10)
    s.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
