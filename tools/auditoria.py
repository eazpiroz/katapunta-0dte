#!/usr/bin/env python3
"""Auditoría diaria del Flight Deck: cada panel contra lo que hizo el precio y contra el perfil γ (Gexbot).

Uso:  python3 tools/auditoria.py CARPETA_HISTORIAL [CARPETA_ARCHIVO] [--tk SPX]
  CARPETA_HISTORIAL: archivos AAAA-MM-DD.jsonl(.gz) del recolector (una lectura por minuto; la rama live los guarda en historial/).
  CARPETA_ARCHIVO:   archivo/AAAA-MM-DD.json del repo; de ahí sale la serie del perfil γ (day.gx) si existe.
Imprime un reporte markdown: el último día y el acumulado de todos los días disponibles.

Notas de método
- El precio es la lectura de cada minuto (no hay máximos/mínimos dentro del minuto: los toques se subestiman algo).
- Cierre = última lectura ≤ 15:00 CDMX.
- Las estructuras se evalúan al vencimiento con el crédito/débito de la lectura (precio medio), por contrato.
  No modelan la salida de las 14:30 ni el deslizamiento.
- Régimen: precio vs zero gamma del perfil γ (si hay serie del día); si no, el signo del GEX 0DTE propio.
"""
import gzip, json, sys, statistics as st
from collections import defaultdict
from pathlib import Path

TOL = {"SPX": 3, "SPY": 0.3, "QQQ": 0.3}
OPEN = 510  # 8:30 CDMX en minutos


def mins(t):
    h, m = str(t).split(":")[:2]
    return int(h) * 60 + int(m)


def load_day(f, tk):
    rs = {}
    op = gzip.open if str(f).endswith(".gz") else open
    for line in op(f, "rt", encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        r = j.get(tk)
        if not r or not r.get("px") or not r.get("t"):
            continue
        m = mins(r["t"]) - OPEN
        if 0 <= m <= 390:
            rs[m] = r
    return rs


def load_gx(arch, day, tk):
    if not arch:
        return None
    f = Path(arch) / f"{day}.json"
    if not f.exists():
        return None
    try:
        gx = json.loads(f.read_text()).get("day", {}).get("gx", {}).get(tk)
    except ValueError:
        return None
    if not gx:
        return None
    return {int(r[0]): {"zg": r[1], "mp": r[2], "mn": r[3]} for r in gx}


def gx_at(gx, m):
    if not gx:
        return None
    b = m // 5
    while b >= 0 and b not in gx:
        b -= 1
    return gx.get(b)


def pct(a, b):
    return f"{a / b:.0%}" if b else "—"


def mean(v):
    return st.mean(v) if v else float("nan")


class Acc:
    """Acumula por día y en total."""
    def __init__(self):
        self.day = defaultdict(list)
        self.tot = defaultdict(list)

    def add(self, key, val, today):
        self.tot[key].append(val)
        if today:
            self.day[key].append(val)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tk = sys.argv[sys.argv.index("--tk") + 1] if "--tk" in sys.argv else "SPX"
    hist = Path(args[0])
    arch = args[1] if len(args) > 1 else None
    tol = TOL[tk]
    files = sorted(hist.glob("*.jsonl*"), key=lambda f: f.name)
    files = [f for f in files if len(load_day(f, tk)) >= 120]
    if not files:
        sys.exit("No hay días con historial suficiente.")
    last = files[-1].name.split(".")[0]
    A = {k: Acc() for k in ("rng", "lvl", "pin", "reg", "mom", "est", "t10", "lad")}
    info = {}
    for f in files:
        day = f.name.split(".")[0]
        today = day == last
        rs = load_day(f, tk)
        M = sorted(rs)
        px = {m: rs[m]["px"] for m in M}
        close = px[M[-1]]
        gx = load_gx(arch, day, tk)

        def regime(m):
            g = gx_at(gx, m)
            r = rs[m]
            if g:
                d = r["px"] - g["zg"]
                return "γ+" if d > tol else "γ−" if d < -tol else "límite"
            d0 = (r.get("reg") or {}).get("d0", 0)
            return "γ+" if d0 > 0 else "γ−" if d0 < 0 else "límite"

        def rest(m):
            return [px[t] for t in M if t >= m]

        def nxt(m, k):
            return [px[t] for t in M if m <= t <= m + k]

        def fut(m, k):
            t = min(M[-1], m + k)
            while t not in px and t > m:
                t -= 1
            return px[t]

        if today:
            first = rs[M[0]]
            hi, lo = max(px.values()), min(px.values())
            info = {"day": day, "close": close, "hi": hi, "lo": lo, "gx": bool(gx),
                    "em": first.get("em"), "n": len(M)}
            mins_reg = defaultdict(int)
            for m in M:
                mins_reg[regime(m)] += 1
            info["reg_mins"] = dict(mins_reg)

        # 1. RANGO (cada 15 min, 8:45–14:30)
        for m in M:
            if m % 15 or m < 15 or m > 360:
                continue
            r, R = rs[m], rest(m)
            reg = regime(m)
            bs = {}
            if r.get("rem"):
                bs["1× restante"] = r["rem"]
            for name, b in zip(("1.5×", "2×"), r.get("bands") or []):
                bs[name] = b
            if r.get("d15") and all(r["d15"]):
                bs["Delta 15"] = r["d15"]
            for name, (blo, bhi) in bs.items():
                A["rng"].add((name, "toca"), min(R) < blo or max(R) > bhi, today)
                A["rng"].add((name, "cierra dentro"), blo <= close <= bhi, today)
                A["rng"].add((name, "cierra dentro " + reg), blo <= close <= bhi, today)

        # 2. TECHO / PISO aguantan 60 min (cada 15 min)
        for m in M:
            if m % 15 or m > 330:
                continue
            r, N = rs[m], nxt(m, 60)
            reg = regime(m)
            for name, ok in (("techo", lambda v: max(N) <= v + tol), ("piso", lambda v: min(N) >= v - tol)):
                v = r.get(name)
                if v:
                    fam = v[1] if len(v) > 1 else "?"
                    A["lvl"].add((name, "todos"), ok(v[0]), today)
                    A["lvl"].add((name, reg), ok(v[0]), today)
                    A["lvl"].add((name, f"{fam} familias"), ok(v[0]), today)

        # 3. PIN vs cierre (cada 5 min desde 12:00)
        for m in M:
            if m % 5 or m < 210:
                continue
            r = rs[m]
            g = gx_at(gx, m)
            slot = "12–13" if m < 270 else "13–14" if m < 330 else "14–14:30" if m < 360 else "14:30–15"
            reg = regime(m)
            cand = {"precio del momento": r["px"]}
            if r.get("pin"):
                cand["PIN del tablero"] = r["pin"][0]
            if r.get("iman"):
                cand["imán (major + propio)"] = r["iman"]
            if r.get("mpv") and r.get("mnv"):
                cand["punto medio majors propios"] = (r["mpv"] + r["mnv"]) / 2
                cand["major − propio"] = r["mnv"]
            if g:
                cand["punto medio majors perfil γ"] = (g["mp"] + g["mn"]) / 2
                cand["major − perfil γ"] = g["mn"]
                cand["major + perfil γ"] = g["mp"]
                cand["zero gamma perfil γ"] = g["zg"]
                cand["regla γ− (major − si γ−; si no, tablero)"] = g["mn"] if reg == "γ−" else cand.get("PIN del tablero", r["px"])
            px_ = r.get("pinx") or {}
            for k, lab in (("mp", "max pain vol"), ("cen", "centro γ vol"), ("near", "strike más cercano")):
                if px_.get(k) is not None:
                    cand[lab] = px_[k]
            for k, v in cand.items():
                if v is not None:
                    A["pin"].add((k, slot), abs(close - v), today)

        # 4. RÉGIMEN vs movimiento (bloques de 15 min)
        for m in M:
            if m % 15 or m > 375:
                continue
            blk = [px[t] for t in M if m <= t < m + 15]
            if len(blk) >= 10:
                A["reg"].add(regime(m), max(blk) - min(blk), today)

        # 5. MOMENTUM 3 TF (cambios de momA a ±1)
        prev = 0
        for m in M:
            s = rs[m].get("momA") or 0
            if s and s != prev and m <= 330:
                p0 = px[m]
                N = nxt(m, 60)
                A["mom"].add("30m", (fut(m, 30) - p0) * s, today)
                A["mom"].add("15m", (fut(m, 15) - p0) * s, today)
                A["mom"].add("mfe", max((x - p0) * s for x in N), today)
                A["mom"].add("mae", max((p0 - x) * s for x in N), today)
            prev = s

        # 6. ESTRUCTURAS al vencimiento (cada 30 min, 9:00–13:30), $ por contrato
        for m in M:
            if m % 30 or m < 30 or m > 300:
                continue
            r = rs[m]
            v = r.get("vs") or {}
            if v.get("bc"):
                s_, l_, cr = v["bc"]
                A["est"].add("bear call (vendido)", (cr - max(0, min(close - s_, l_ - s_))) * 100, today)
            if v.get("bp"):
                s_, l_, cr = v["bp"]
                A["est"].add("bull put (vendido)", (cr - max(0, min(s_ - close, s_ - l_))) * 100, today)
            if v.get("cc"):
                l_, s_, db = v["cc"]
                A["est"].add("call debit (comprado)", (max(0, min(close - l_, s_ - l_)) - db) * 100, today)
            if v.get("pp"):
                l_, s_, db = v["pp"]
                A["est"].add("put debit (comprado)", (max(0, min(l_ - close, l_ - s_)) - db) * 100, today)
            for name in ("icd", "ic2", "ic"):
                ic = r.get(name)
                if ic and len(ic) >= 4 and ic[2]:
                    ps, cs, cr, w = ic[:4]
                    loss = max(0, min(ps - close, w)) + max(0, min(close - cs, w))
                    lab = {"icd": "IC Delta 15", "ic2": "IC ajustado al régimen", "ic": "IC rango restante"}[name]
                    A["est"].add(lab, (cr - loss) * 100, today)

        # 8. ESCALERA de verticales (venta vs compra) por régimen, al vencimiento, precio mid, cada 30 min 9:00–13:30
        for m in M:
            if m % 30 or m < 30 or m > 300:
                continue
            lad = rs[m].get("lad") or {}
            rg = regime(m)
            for side, dl, s_, l_, nat, md in lad.get("v", []):
                if not md or md <= 0:
                    continue
                w = abs(l_ - s_)
                itm = (close - s_) if side == "c" else (s_ - close)
                lab = f"venta {'bear call' if side == 'c' else 'bull put'} δ{int(dl * 100)} ×{w:g}"
                A["lad"].add((lab, rg), (md - max(0, min(itm, w))) * 100, today)
            for side, dl, l_, s_, nat, md in lad.get("c", []):
                if not md or md <= 0:
                    continue
                w = abs(s_ - l_)
                itm = (close - l_) if side == "c" else (l_ - close)
                lab = f"compra {'bull call' if side == 'c' else 'bear put'} δ{int(dl * 100)} ×{w:g}"
                A["lad"].add((lab, rg), (max(0, min(itm, w)) - md) * 100, today)

        # 7. SEMÁFORO TOP 10 (cambios de estado) → SPX 30 min después
        prev = None
        for m in M:
            t = rs[m].get("t10")
            if not t or m > 360:
                continue
            s = t.get("sem")
            if s != prev and s:
                A["t10"].add("verde" if s > 0 else "rojo", (fut(m, 30) - px[m]) * s, today)
            prev = s

    nd = len(files)
    P = print
    P(f"# Auditoría Flight Deck · {tk} · {info['day']}")
    P()
    P(f"Cierre {info['close']:,.2f} · máx {info['hi']:,.2f} · mín {info['lo']:,.2f} · rango {info['hi'] - info['lo']:.1f} pts · "
      f"{info['n']} lecturas · perfil γ {'sí' if info['gx'] else 'no (régimen propio)'} · acumulado: {nd} días ({files[0].name.split('.')[0]} a {last})")
    if info.get("em"):
        e = info["em"]
        P(f"Rango de apertura (fijo) {e[0]:,.0f}–{e[1]:,.0f}: {'contuvo el día' if e[0] <= info['lo'] and info['hi'] <= e[1] else 'se rompió'}"
          f" (máx {info['hi'] - e[1]:+.1f} / mín {info['lo'] - e[0]:+.1f} vs bordes).")
    rm = info.get("reg_mins", {})
    P("Minutos por régimen hoy: " + " · ".join(f"{k} {v}" for k, v in sorted(rm.items())))
    P()

    def tbl(title, header, rows):
        P(f"## {title}")
        P("| " + " | ".join(header) + " |")
        P("|" + "---|" * len(header))
        for r in rows:
            P("| " + " | ".join(str(x) for x in r) + " |")
        P()

    rows = []
    for key in sorted({k for k in A["rng"].tot}, key=lambda k: (k[0], k[1])):
        d, t = A["rng"].day.get(key, []), A["rng"].tot[key]
        rows.append([key[0], key[1], f"{pct(sum(d), len(d))} (n {len(d)})", f"{pct(sum(t), len(t))} (n {len(t)})"])
    tbl("1. Rango (muestras cada 15 min)", ["banda", "medida", "hoy", "acumulado"], rows)

    rows = []
    for key in sorted(A["lvl"].tot):
        d, t = A["lvl"].day.get(key, []), A["lvl"].tot[key]
        rows.append([key[0], key[1], f"{pct(sum(d), len(d))} (n {len(d)})", f"{pct(sum(t), len(t))} (n {len(t)})"])
    tbl(f"2. Techo / piso aguantan 60 min (tolerancia {tol})", ["nivel", "grupo", "hoy", "acumulado"], rows)

    slots = ["12–13", "13–14", "14–14:30", "14:30–15"]
    cands = sorted({k[0] for k in A["pin"].tot})
    rows = []
    for c in cands:
        rows.append([c] + [f"{mean(A['pin'].day.get((c, s), [])):.1f}" if A["pin"].day.get((c, s)) else "—" for s in slots]
                    + [f"{mean([x for s in slots for x in A['pin'].tot.get((c, s), [])]):.1f}"])
    rows.sort(key=lambda r: float(r[-2]) if r[-2] != "—" else 999)
    tbl("3. PIN: error medio vs cierre (pts) — hoy por franja; última columna = acumulado 12–15 h",
        ["candidato"] + slots + ["acum."], rows)

    rows = []
    for key in ("γ+", "límite", "γ−"):
        d, t = A["reg"].day.get(key, []), A["reg"].tot.get(key, [])
        if t:
            rows.append([key, f"{mean(d):.1f} (n {len(d)})" if d else "—", f"{mean(t):.1f} (n {len(t)})"])
    tbl("4. Régimen vs movimiento: rango medio de cada bloque de 15 min (pts)", ["régimen al inicio", "hoy", "acumulado"], rows)

    def momrow(acc):
        n = len(acc.get("30m", []))
        if not n:
            return ["—"] * 5
        return [n, pct(sum(x > 0 for x in acc["30m"]), n), f"{mean(acc['15m']):+.1f}", f"{mean(acc['30m']):+.1f}",
                f"{mean(acc['mfe']):.1f} / {mean(acc['mae']):.1f}"]
    tbl("5. Momentum fuerte 3 TF (señales nuevas)", ["", "señales", "acierto 30m", "mov. 15m", "mov. 30m", "a favor / en contra 60m"],
        [["hoy"] + momrow(A["mom"].day), ["acumulado"] + momrow(A["mom"].tot)])

    rows = []
    for key in sorted(A["est"].tot):
        d, t = A["est"].day.get(key, []), A["est"].tot[key]
        rows.append([key, f"{pct(sum(x > 0 for x in d), len(d))} · {mean(d):+.0f} $ (n {len(d)})" if d else "—",
                     f"{pct(sum(x > 0 for x in t), len(t))} · {mean(t):+.0f} $ (n {len(t)})"])
    tbl("6. Estructuras sugeridas, al vencimiento (cada 30 min 9:00–13:30; $ por contrato, sin comisiones)",
        ["estructura", "hoy: gana · P&L medio", "acumulado"], rows)

    if A["t10"].tot:
        rows = []
        for key in ("verde", "rojo"):
            d, t = A["t10"].day.get(key, []), A["t10"].tot.get(key, [])
            if t:
                rows.append([key, f"{mean(d):+.1f} (n {len(d)})" if d else "—", f"{mean(t):+.1f} (n {len(t)})",
                             pct(sum(x > 0 for x in t), len(t))])
        tbl("7. Semáforo Top 10: SPX 30 min después de cada cambio, a favor del color (pts)", ["color", "hoy", "acumulado", "acierto acum."], rows)

    if A["lad"].tot:
        regs = ("γ+", "límite", "γ−")
        cell = lambda v: f"{pct(sum(x > 0 for x in v), len(v))} · {mean(v):+.0f} $ (n {len(v)})" if v else "—"
        labs = sorted({k[0] for k in A["lad"].tot}, key=lambda s: (s.split()[0], s.split()[1], int(s.split('δ')[1].split()[0]), s))
        rows = [[lab] + [cell(A["lad"].tot.get((lab, rg), [])) for rg in regs] for lab in labs]
        tbl("8. Escalera venta vs compra por régimen, acumulado (al vencimiento, precio mid, cada 30 min 9:00–13:30; gana · P&L medio $/contrato)",
            ["estructura"] + list(regs), rows)
        rows = [[lab] + [cell(A["lad"].day.get((lab, rg), [])) for rg in regs] for lab in labs if any(A["lad"].day.get((lab, rg)) for rg in regs)]
        if rows:
            tbl("8b. Escalera — solo hoy", ["estructura"] + list(regs), rows)


if __name__ == "__main__":
    main()
