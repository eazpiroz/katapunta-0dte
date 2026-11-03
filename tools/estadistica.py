#!/usr/bin/env python3
"""Estadística del Flight Deck con el historial del recolector (una lectura por minuto).

Uso:  python estadistica.py CARPETA_HISTORIAL [SPX|SPY|QQQ]
      (en la PC del socio: python estadistica.py historial)

Mide, con muestras cada 15 min (sin ver el futuro al decidir):
  - Rango: % que TOCÓ y % que CERRÓ dentro de las bandas 1×, 1.5× y 2× straddle restante y del borde δ15.
  - Niveles: % que aguantó el techo y el piso en los 60 min siguientes (tolerancia SPX 3 / ETF 0.3),
    y distancia del PIN al cierre. Todo separado por régimen 0DTE (γ+ / γ− / incierto) y por familias del nivel.
El historial guarda el precio cada minuto; los extremos intraminuto no se ven (subestima algo los toques).
"""
import json, sys
from collections import defaultdict
from pathlib import Path

TOL = {"SPX": 3, "SPY": 0.3, "QQQ": 0.3}


def load(day_file, tk):
    out = []
    for line in open(day_file, encoding="utf-8"):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        r = j.get(tk)
        if r and r.get("px"):
            out.append(r)
    return out


def regime(r):
    g = r.get("reg") or {}
    if g.get("inc"):
        return "incierto"
    return "γ+" if g.get("d0", 0) > 0 else "γ−" if g.get("d0", 0) < 0 else "incierto"


def main():
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "historial")
    tk = sys.argv[2] if len(sys.argv) > 2 else "SPX"
    tol = TOL[tk]
    band = defaultdict(lambda: [0, 0, 0])            # (banda, régimen) → [n, tocó, cerró dentro]
    lvl = defaultdict(lambda: [0, 0])                # (nivel, régimen, familias) → [n, aguantó]
    pin = defaultdict(list)                          # régimen → distancias al cierre
    days = sorted(folder.glob("*.jsonl"))
    for f in days:
        rs = load(f, tk)
        if len(rs) < 30:
            continue
        pxs = [r["px"] for r in rs]
        close = pxs[-1]
        for i in range(0, len(rs) - 1, 15):
            r = rs[i]
            rest = pxs[i:]
            nxt = pxs[i:i + 60]
            reg = regime(r)
            bs = {}
            if r.get("rem"):
                bs["1×"] = r["rem"]
            for name, b in zip(("1.5×", "2×"), r.get("bands") or []):
                bs[name] = b
            if r.get("d15") and all(r["d15"]):
                bs["δ15"] = r["d15"]
            for name, (lo, hi) in bs.items():
                a = band[(name, reg)]
                a[0] += 1
                a[1] += min(rest) < lo or max(rest) > hi
                a[2] += lo <= close <= hi
            for name, key, ok in (("techo", "techo", lambda v: max(nxt) <= v + tol),
                                  ("piso", "piso", lambda v: min(nxt) >= v - tol)):
                v = r.get(key)
                if v:
                    fam = v[1] if len(v) > 2 else "?"
                    a = lvl[(name, reg, fam)]
                    a[0] += 1
                    a[1] += ok(v[0])
            if r.get("pin"):
                pin[reg].append(abs(close - r["pin"][0]))
    print(f"{tk} · {len(days)} días de historial\n")
    print("RANGO (muestras cada 15 min)            n   tocó   cerró dentro")
    for (name, reg), (n, t, c) in sorted(band.items()):
        print(f"  {name:5} {reg:9}                 {n:5} {t / n:6.0%} {c / n:9.0%}")
    print("\nNIVELES (aguanta 60 min)   familias      n   aguantó")
    for (name, reg, fam), (n, ok) in sorted(lvl.items(), key=lambda x: (x[0][0], x[0][1], str(x[0][2]))):
        print(f"  {name:6} {reg:9}        {fam!s:>3}       {n:5} {ok / n:7.0%}")
    print("\nPIN → cierre (distancia mediana)")
    for reg, ds in sorted(pin.items()):
        ds = sorted(ds)
        print(f"  {reg:9} n {len(ds):4}  mediana {ds[len(ds) // 2]:.1f}  dentro de {tol * 3:g}: {sum(d <= tol * 3 for d in ds) / len(ds):.0%}")


if __name__ == "__main__":
    main()
