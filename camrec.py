#!/usr/bin/env python3
"""camrec - record the camera object while you do a fixed sequence of things, to find what the game
uses to auto-centre the camera (a "time since the last stick input" timer, an on/off flag, a target
angle, a return speed).

    camrec.py record --spec "[0x00913964]+0x180" -o rec.npz     # then do what the console asks
    camrec.py analyze rec.npz                                    # what in the object reacts to what

The memory window around the yaw field (the whole camera object by default) is sampled ~100 times a
second while a script of phases runs (idle / camera input / walking / tilting).  A beep marks every
phase change; the phase list is printed first, so a second screen is not needed.

Do the recording with KAMI OFF and use the gamepad stick (or the key bindings of the game's own camera),
not the mouse: the point is to see what the game does when it *does* get camera input and when it does not.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Tuple

import numpy as np

from rpcs3mem import add_backend_args, open_backend, parse_int
from rpcs3mem.common import MemoryError_
from watch import Spec

DEFAULT_SCRIPT = "idle:6,input:6,idle:6,walk:8,idle:5,tilt:4,walkf:8,idle:6"
PHASE_TEXT = {
    "idle": "ПОКОЙ: ничего не трогайте (ни стики, ни кнопки), персонаж стоит",
    "input": "КАМЕРА: крутите камеру вправо-влево правым стиком (мышь и KAMI не используйте), персонаж стоит",
    "walk": "ХОДЬБА ВБОК: идите левым стиком вбок или по кругу, камеру НЕ трогайте (камера будет сама доворачиваться)",
    "tilt": "НАКЛОН: наклоните камеру вниз до упора правым стиком и отпустите; персонаж стоит, дальше ничего не трогайте",
    "walkf": "ХОДЬБА ВПЕРЁД: идите вперёд левым стиком, камеру НЕ трогайте (наклон должен сам вернуться к середине)",
    "focus": "ВНИМАНИЕ: нажмите кнопку «внимание» (фокус камеры на цели или интересном объекте), нажимайте и отпускайте, как в игре",
    "mouse": "МЫШЬ (KAMI ВКЛЮЧЁН): отверните камеру мышью в сторону от того, куда смотрит персонаж, и остановитесь",
    "aim": "ПРИЦЕЛ: зажмите кнопку прицеливания и держите, камеру не трогайте",
    "aimmove": "ПРИЦЕЛ + КАМЕРА: держите прицел и крутите камеру правым стиком влево-вправо и вверх-вниз",
    "wall": "СТЕНА (стик): встаньте спиной к стене или в угол и правым стиком крутите камеру В стену, держите (KAMI выключен)",
    "wallm": "СТЕНА (мышь): встаньте спиной к стене или в угол и мышью крутите камеру В стену, держите (KAMI ВКЛЮЧЁН)",
}


def parse_script(text: str) -> List[Tuple[str, float]]:
    out = []
    for part in text.split(","):
        name, _, secs = part.strip().partition(":")
        if name not in PHASE_TEXT:
            raise SystemExit(f"неизвестная фаза {name!r}; допустимы: {', '.join(PHASE_TEXT)}")
        out.append((name, float(secs)))
    return out


NO_BEEP = False


def beep(n: int = 1) -> None:
    if NO_BEEP:
        return
    try:
        import winsound
        for _ in range(n):
            winsound.Beep(1000, 120)
            time.sleep(0.05)
    except Exception:
        print("\a", end="", flush=True, file=sys.stderr)


# --------------------------------------------------------------------------
def cmd_record(args) -> int:
    global NO_BEEP
    NO_BEEP = args.no_beep
    phases = parse_script(args.script)
    spec = Spec(args.spec)
    mem = open_backend(args)
    try:
        addr = spec.resolve(mem)
        if addr is None:
            print("цепочка не разрешается: объект камеры сейчас не найден (меню? загрузка?)", file=sys.stderr)
            return 2
        start = (addr - args.before) & ~3
        size = (args.before + args.after + 3) & ~3
        print(f"поле {spec.name} сейчас по адресу 0x{addr:08X}; окно 0x{start:08X}..0x{start + size:08X} "
              f"({size // 4} слов), {args.rate:.0f} отсчётов в секунду", file=sys.stderr)
        print("\nСценарий (звуковой сигнал при каждой смене):", file=sys.stderr)
        for i, (name, secs) in enumerate(phases, 1):
            print(f"  {i}. {secs:4.0f} c  {PHASE_TEXT[name]}", file=sys.stderr)
        print(f"\nЗапись начнётся через {args.delay:.0f} с после трёх сигналов: перейдите в окно игры "
              f"(игра должна быть в обычном режиме, не в меню).", file=sys.stderr)
        time.sleep(args.delay)
        beep(3)
        time.sleep(0.5)

        times, rows, bounds, addrs = [], [], [], []
        period = 1.0 / args.rate
        cur = addr
        t0 = time.perf_counter()
        for i, (name, secs) in enumerate(phases):
            bounds.append((name, time.perf_counter() - t0))
            print(f"[{i + 1}/{len(phases)}] {PHASE_TEXT[name]}", file=sys.stderr, flush=True)
            beep(1)
            t_end = time.perf_counter() + secs
            while time.perf_counter() < t_end:
                t = time.perf_counter()
                # the chain is resolved on every sample: when the game switches to another camera object
                # (aiming, vehicles) the window follows it, and the change is recorded
                new = spec.resolve(mem)
                if new is not None and new != cur:
                    print(f"    адрес по цепочке изменился: 0x{cur:08X} -> 0x{new:08X} ({t - t0:.2f} c)",
                          file=sys.stderr, flush=True)
                    cur = new
                try:
                    rows.append(np.frombuffer(mem.read((cur - args.before) & ~3, size), dtype=">u4").astype(np.uint32))
                except MemoryError_ as e:
                    print(f"чтение не удалось: {e}", file=sys.stderr)
                    return 1
                times.append(t - t0)
                addrs.append(cur)
                dt = period - (time.perf_counter() - t)
                if dt > 0:
                    time.sleep(dt)
        beep(2)
        words = np.vstack(rows)
        np.savez_compressed(args.output, t=np.array(times), words=words, start=start, yaw_addr=addr,
                            yaw_addrs=np.array(addrs, dtype=np.uint32),
                            phase_names=np.array([b[0] for b in bounds]),
                            phase_start=np.array([b[1] for b in bounds]),
                            phase_end=np.array([b[1] + p[1] for b, p in zip(bounds, phases)]),
                            spec=args.spec)
        print(f"\nзаписано {len(times)} отсчётов ({times[-1]:.0f} с) в {args.output}", file=sys.stderr)
        print(f"Дальше: py -3 camrec.py analyze {args.output}", file=sys.stderr)
    finally:
        mem.close()
    return 0


# --------------------------------------------------------------------------
def _fit(tt: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """slope and R^2 of a straight line through (tt, y)."""
    if len(tt) < 4:
        return 0.0, 0.0
    A = np.vstack((tt - tt[0], np.ones(len(tt)))).T
    sol, res, *_ = np.linalg.lstsq(A, y, rcond=None)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot <= 0:
        return float(sol[0]), 0.0
    ss_res = float(((y - A @ sol) ** 2).sum())
    return float(sol[0]), 1.0 - ss_res / ss_tot


def analyze(path: str, top: int = 25) -> int:
    d = np.load(path, allow_pickle=True)
    t, W = d["t"], d["words"]
    start, yaw_addr = int(d["start"]), int(d["yaw_addr"])
    names = [str(x) for x in d["phase_names"]]
    p_start, p_end = d["phase_start"], d["phase_end"]
    F = W.view(np.float32)
    n_ph = len(names)
    sel = [(t >= p_start[i]) & (t < p_end[i]) for i in range(n_ph)]
    print(f"{path}: {len(t)} отсчётов, {W.shape[1]} слов, поле yaw по адресу 0x{yaw_addr:08X}")
    print("фазы: " + " | ".join(f"{i + 1}:{names[i]}" for i in range(n_ph)))
    if "yaw_addrs" in d.files:
        ya = d["yaw_addrs"]
        jumps = np.flatnonzero(ya[1:] != ya[:-1]) + 1
        if len(jumps):
            print("ЦЕЛЬ ЦЕПОЧКИ МЕНЯЛАСЬ (игра переключила объект камеры):")
            for j in jumps[:20]:
                ph = int(np.searchsorted(p_start, t[j], side="right") - 1)
                print(f"   {t[j]:6.2f} c  фаза {ph + 1}:{names[ph]}  0x{int(ya[j - 1]):08X} -> 0x{int(ya[j]):08X}")
        else:
            print("цель цепочки не менялась: тот же объект камеры в течение всей записи")

    def off(j: int) -> str:
        o = start + 4 * j - yaw_addr
        return f"{'+' if o >= 0 else '-'}0x{abs(o):X}"

    changed_any = (W[1:] != W[:-1]).any(axis=0)
    cols = np.flatnonzero(changed_any)
    print(f"меняются {len(cols)} из {W.shape[1]} слов\n")

    with np.errstate(all="ignore"):
        act = np.zeros((len(cols), n_ph))                       # fraction of steps where the word changed
        for i in range(n_ph):
            s = W[sel[i]]
            act[:, i] = (s[1:, cols] != s[:-1, cols]).mean(axis=0) if len(s) > 1 else 0
    glyph = lambda a: "." if a < 0.02 else "-" if a < 0.2 else "+" if a < 0.6 else "#"

    # --- words whose activity depends on the phase: neither noise / a frame counter (active all the time) nor
    #     a plain camera angle (active only while you give camera input)
    is_input = np.array([n == "input" for n in names])
    print("== Слова, активность которых зависит от фазы (не шум и не сам угол камеры): ==")
    print("   смещ.      тип    мин .. макс                  активность по фазам")
    rows = []
    for k, j in enumerate(cols):
        always = bool((act[k] >= 0.2).all())
        only_input = bool((act[k][~is_input] < 0.02).all())
        if not always and not only_input and act[k].max() >= 0.2:
            col = F[:, j]
            ok = np.isfinite(col) & (np.abs(col) < 1e6)
            kind = "float" if ok.mean() > 0.95 and np.abs(col[ok]).max() > 1e-30 else "int"
            lo, hi = (float(col[ok].min()), float(col[ok].max())) if kind == "float" else (int(W[:, j].min()), int(W[:, j].max()))
            rows.append((float(act[k].sum()), off(j), kind, lo, hi, "".join(glyph(a) for a in act[k])))
    for r in sorted(rows, reverse=True)[:top]:
        lo, hi = (f"{r[3]:12.5f}", f"{r[4]:12.5f}") if r[2] == "float" else (f"{r[3]:12d}", f"{r[4]:12d}")
        print(f"   {r[1]:8s}   {r[2]:5s}  {lo} .. {hi}   {r[5]}")
    if not rows:
        print("   (нет)")

    # --- words that move only while the character walks (the camera follows the character)
    is_walk = np.array([n.startswith("walk") for n in names])
    is_rest = np.array([n == "idle" for n in names])
    if is_walk.any() and is_rest.any():
        print("\n== Меняются только при ходьбе (в покое неподвижны): ==")
        print("   смещ.      тип    мин .. макс                  активность по фазам")
        rows = []
        for k, j in enumerate(cols):
            if act[k][is_walk].max() >= 0.2 and act[k][is_rest].max() < 0.02:
                col = F[:, j]
                ok = np.isfinite(col) & (np.abs(col) < 1e6)
                kind = "float" if ok.mean() > 0.95 and np.abs(col[ok]).max() > 1e-30 else "int"
                lo, hi = (float(col[ok].min()), float(col[ok].max())) if kind == "float" else (int(W[:, j].min()), int(W[:, j].max()))
                rows.append((float(act[k][is_walk].sum()), off(j), kind, lo, hi, "".join(glyph(a) for a in act[k])))
        for r in sorted(rows, reverse=True)[:top]:
            lo, hi = (f"{r[3]:12.5f}", f"{r[4]:12.5f}") if r[2] == "float" else (f"{r[3]:12d}", f"{r[4]:12d}")
            print(f"   {r[1]:8s}   {r[2]:5s}  {lo} .. {hi}   {r[5]}")
        if not rows:
            print("   (нет)")

    # --- timers: straight ramps, with a drop (reset) somewhere
    print("\n== Таймеры: значение линейно растёт или убывает, потом сбрасывается ==")
    print("   смещ.      скорость/с   фаза роста        сброс (фаза, значение до -> после)")
    found = []
    for j in cols:
        col = F[:, j].astype(np.float64)
        if not np.isfinite(col).all() or np.abs(col).max() > 1e6:
            col = W[:, j].astype(np.float64)                     # counters kept as integers
        ramps = []
        for i in range(n_ph):
            s = sel[i]
            if s.sum() < 30 or (p_end[i] - p_start[i]) < 2.5:
                continue
            slope, r2 = _fit(t[s], col[s])
            rng = float(col[s].max() - col[s].min())
            if r2 > 0.99 and abs(slope) > 1e-3 and rng > 0.05 * max(1e-9, abs(slope)) * 2:
                ramps.append((i, slope))
        if not ramps:
            continue
        step = np.diff(col)
        amp = float(col.max() - col.min())
        jumps = np.flatnonzero(np.abs(step) > 0.25 * amp) if amp > 0 else []
        if len(ramps) < 2 and not len(jumps):            # one straight run without a reset: just a moving angle
            continue
        found.append((len(ramps) + (2 if len(jumps) else 0), off(j), ramps, jumps, col))
    for score, o, ramps, jumps, col in sorted(found, key=lambda x: -x[0])[:top]:
        r = ", ".join(f"{i + 1}:{names[i]}" for i, _ in ramps)
        sl = ramps[0][1]
        reset = ""
        if len(jumps):
            q = jumps[0]
            ph = next((i for i in range(n_ph) if p_start[i] <= t[q] < p_end[i]), -1)
            reset = f"{ph + 1}:{names[ph]}  {col[q]:.3f} -> {col[q + 1]:.3f}"
        print(f"   {o:8s}   {sl:+10.4f}   {r:16s}  {reset}")
    if not found:
        print("   (нет)")

    # --- flags: a few distinct values with transitions
    print("\n== Флаги: слово принимает всего несколько значений и переключается ==")
    print("   смещ.      значения (hex)                       доля отсчётов по фазам для каждого значения")
    flag_rows = []
    for j in cols:
        vals, counts = np.unique(W[:, j], return_counts=True)
        if 2 <= len(vals) <= 4 and (W[1:, j] != W[:-1, j]).sum() <= 40:
            per = []
            for v in vals:
                per.append("".join(glyph(float((W[sel[i], j] == v).mean())) for i in range(n_ph)))
            flag_rows.append((off(j), vals, per))
    for o, vals, per in flag_rows[:top]:
        print(f"   {o:8s}   " + "  ".join(f"{int(v):08X}" for v in vals) + "   " + " / ".join(per))
    if not flag_rows:
        print("   (нет)")
    print("\nУсловные знаки активности: . почти не меняется   - редко   + часто   # почти каждый отсчёт")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record the camera object while a fixed script runs")
    add_backend_args(r)
    r.add_argument("--spec", required=True, help="address or pointer chain of the yaw field, e.g. [0x00913964]+0x180")
    r.add_argument("--before", type=parse_int, default=0x1C0, help="bytes before the field (default 0x1C0)")
    r.add_argument("--after", type=parse_int, default=0x680, help="bytes from the field on (default 0x680)")
    r.add_argument("--rate", type=float, default=100.0, help="samples per second")
    r.add_argument("--delay", type=float, default=8.0, help="seconds to switch to the game before recording")
    r.add_argument("--script", default=DEFAULT_SCRIPT, help=f"phases name:seconds, default {DEFAULT_SCRIPT}")
    r.add_argument("--no-beep", action="store_true", help="no sounds (tests)")
    r.add_argument("-o", "--output", default="camrec.npz")
    r.set_defaults(func=cmd_record)
    a = sub.add_parser("analyze", help="summarise a recording")
    a.add_argument("file")
    a.add_argument("--top", type=int, default=25)
    a.set_defaults(func=lambda args: analyze(args.file, args.top))
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
