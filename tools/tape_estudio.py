"""¿El tape anticipa al SPX? Reacción del precio después del flujo grande (Tape v2, 21-nov).

Lo usan calificaciones.py (columna "Tape" del scorecard) y revision_bot.py (revisión diaria).
Eventos de cada día (de historial/tape_min_FECHA.jsonl[.gz], que guarda el bot de papel):
  · bloques acumulados (Tape v2)
  · prints sueltos de +PRIMA_MIN en prima, sin "medio" y sin los muy ITM (cierres y coberturas)
Dirección del evento: compra de calls o venta de puts = alcista; venta de calls o compra de puts = bajista.
Se mide si el SPX se movió a favor 15 y 30 min después (precio del historial del recolector).
Solo evidencia: no es señal de entrada.

    python3 -I tools/tape_estudio.py CARPETA_HISTORIAL     # tabla por día y acumulado
"""
import gzip, json, sys
from pathlib import Path

PRIMA_MIN = 400_000      # ~percentil 90 de los prints de +100 contratos (17–20 nov)
ITM = 30


def _jsonl(f):
    op = gzip.open if str(f).endswith(".gz") else open
    out = []
    with op(f, "rt", encoding="utf-8") as fh:
        for x in fh:
            try:
                out.append(json.loads(x))
            except ValueError:
                pass
    return out


def leer_tape(carpeta, fecha):
    for f in (Path(carpeta) / f"tape_min_{fecha}.jsonl", Path(carpeta) / f"tape_min_{fecha}.jsonl.gz"):
        if f.exists():
            return _jsonl(f)
    return []


def leer_precios(carpeta, fecha):
    """[(minuto CDMX, px SPX)] del historial del recolector."""
    for f in (Path(carpeta) / f"{fecha}.jsonl", Path(carpeta) / f"{fecha}.jsonl.gz"):
        if f.exists():
            out = []
            for j in _jsonl(f):
                r = j.get("SPX") or {}
                if r.get("t") and r.get("px"):
                    h, m = r["t"].split(":")[:2]
                    out.append((int(h) * 60 + int(m), float(r["px"])))
            return out
    return []


def _px(precios, m):
    for t, px in precios:
        if t >= m:
            return px
    return None


def direccion(t, lado):
    if lado not in ("B", "V"):
        return 0
    return (1 if lado == "B" else -1) * (1 if t == "C" else -1)


def eventos(lineas):
    ev = []
    for ln in lineas:
        h, m = ln["ts"].split(":")[:2]
        mi, spot = int(h) * 60 + int(m), ln.get("spot")
        for b in ln.get("bloques_nuevos") or []:
            ev.append({"m": mi, "tipo": "bloque", "k": b["k"], "t": b["t"], "dir": direccion(b["t"], b["lado"]),
                       "prem": b["prem"], "fuerte": b.get("agr") == "+"})
        for p in ln.get("nuevos") or []:
            if p.get("spread") or p.get("prem", 0) < PRIMA_MIN or p.get("lado") == "M":
                continue
            if spot and ((p["t"] == "C" and p["k"] < spot - ITM) or (p["t"] == "P" and p["k"] > spot + ITM)):
                continue
            ev.append({"m": mi, "tipo": "print", "k": p["k"], "t": p["t"], "dir": direccion(p["t"], p["lado"]),
                       "prem": p["prem"], "fuerte": str(p.get("agr", "")).endswith("+")})
    return [e for e in ev if e["dir"]]


def reaccion(carpeta, fecha):
    """Resumen del día o None si no hay tape o precios."""
    lineas, precios = leer_tape(carpeta, fecha), leer_precios(carpeta, fecha)
    if not lineas or not precios:
        return None
    fin = precios[-1][0]
    res = {"n": 0, "f15": 0, "f30": 0, "pts15": 0.0, "bloques": 0, "fuertes": 0, "fuertes_f15": 0}
    for e in eventos(lineas):
        if e["m"] + 30 > fin:
            continue
        p0, p15, p30 = _px(precios, e["m"]), _px(precios, e["m"] + 15), _px(precios, e["m"] + 30)
        if None in (p0, p15, p30):
            continue
        a15, a30 = e["dir"] * (p15 - p0), e["dir"] * (p30 - p0)
        res["n"] += 1
        res["f15"] += a15 > 0
        res["f30"] += a30 > 0
        res["pts15"] += a15
        res["bloques"] += e["tipo"] == "bloque"
        if e["fuerte"]:
            res["fuertes"] += 1
            res["fuertes_f15"] += a15 > 0
    if not res["n"]:
        return {"n": 0}
    return {"n": res["n"], "bloques": res["bloques"],
            "a_favor_15": round(100 * res["f15"] / res["n"]), "a_favor_30": round(100 * res["f30"] / res["n"]),
            "pts15": round(res["pts15"] / res["n"], 1),
            "fuertes": res["fuertes"], "fuertes_15": round(100 * res["fuertes_f15"] / res["fuertes"]) if res["fuertes"] else None}


def main(carpeta=None):
    carpeta = Path(carpeta or sys.argv[1])
    fechas = sorted({f.name[9:19] for f in carpeta.glob("tape_min_20??-??-??.jsonl*")})
    print("| Día | Eventos | Bloques | A favor 15 min | A favor 30 min | Pts a favor (15 min) |\n|---|---|---|---|---|---|")
    tot = {"n": 0, "f15": 0, "f30": 0, "pts": 0.0}
    for f in fechas:
        r = reaccion(carpeta, f)
        if not r or not r["n"]:
            print(f"| {f[5:]} | 0 | | | | |")
            continue
        print(f"| {f[5:]} | {r['n']} | {r['bloques']} | {r['a_favor_15']}% | {r['a_favor_30']}% | {r['pts15']:+.1f} |")
        tot["n"] += r["n"]; tot["f15"] += r["a_favor_15"] * r["n"] / 100
        tot["f30"] += r["a_favor_30"] * r["n"] / 100; tot["pts"] += r["pts15"] * r["n"]
    if tot["n"]:
        print(f"| **Acumulado** | {tot['n']} | | {100 * tot['f15'] / tot['n']:.0f}% | {100 * tot['f30'] / tot['n']:.0f}% | {tot['pts'] / tot['n']:+.1f} |")
    print(f"\nEvento = bloque acumulado o print suelto de +${PRIMA_MIN:,} (sin medio ni ITM > {ITM} pts). "
          "50% = moneda al aire; el tape aporta solo si el acumulado se separa de 50% con varios días.")


if __name__ == "__main__":
    main()
