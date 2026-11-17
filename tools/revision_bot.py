#!/usr/bin/env python3
"""Datos para la revisión diaria del bot de papel (la lee la tarea programada de Claude al cierre).

    python3 -I tools/revision_bot.py FECHA CARPETA_HISTORIAL [papel.json] [bitacora.json]

CARPETA_HISTORIAL tiene FECHA.jsonl(.gz) y, si existe, tape_min_FECHA.jsonl(.gz).
Imprime en markdown: el día del bot, el régimen minuto a minuto (cada 15 min), el tape en cada entrada,
la comparación con la Bitácora real y una prueba de sensibilidad del Playbook (mismo día con variantes).
No recomienda órdenes; da evidencia para afinar reglas."""
import gzip, json, os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import papel_bot as P

fecha, carpeta = sys.argv[1], Path(sys.argv[2])
P.HIST = carpeta
papel = json.load(open(sys.argv[3], encoding="utf-8")) if len(sys.argv) > 3 and os.path.exists(sys.argv[3]) else {}
bit = json.load(open(sys.argv[4], encoding="utf-8")) if len(sys.argv) > 4 and os.path.exists(sys.argv[4]) else {}
usd = lambda v: ("−" if v < 0 else "+") + f"${abs(v):,.0f}"


def leer_tape():
    for f in (carpeta / f"tape_min_{fecha}.jsonl", carpeta / f"tape_min_{fecha}.jsonl.gz"):
        if f.exists():
            t = gzip.decompress(f.read_bytes()).decode() if f.suffix == ".gz" else f.read_text(encoding="utf-8")
            return [json.loads(x) for x in t.splitlines() if x.strip()]
    return []


filas = P.leer_dia(fecha)
if not filas:
    print(f"Sin historial para {fecha}."); sys.exit()
print(f"# Revisión del bot · {fecha}\n")

# 1. lo que hizo el bot (vivo si está en papel.json; si no, repetición)
dia = (papel.get("dias") or {}).get(fecha)
if not dia:
    b = P.correr_dia(fecha)
    dia = {**b.resumen(), "operaciones": b.ops, "modo": "repetición"}
print(f"## Bot ({dia.get('modo')}): {usd(dia['pnl'])} · {dia['ops']} ops · {dia['ganadoras']} ganadoras")
if dia.get("nota"):
    print(f"⚠ {dia['nota']}")
for o in dia["operaciones"]:
    c = o.get("ctx", {})
    tp = c.get("tape") or {}
    print(f"- {o['entra']}→{o['sale']} ET · {o['estructura']} · {o['entrada']}→{o['salida']} · **{usd(o['pnl'])}** "
          f"(mejor {usd(o['mejor'])}, peor {usd(o['peor'])}) · {o['regla'][:1]} · salida: {o['motivo']}")
    print(f"  ctx: SPX {c.get('px')} zg {c.get('zg')} mp {c.get('mp')} mn {c.get('mn')} tf2 {c.get('tf2')}"
          + (f" · tape neto C {tp.get('neto', {}).get('C')} P {tp.get('neto', {}).get('P')} flujo5 {tp.get('flujo5')}" if tp else " · sin tape"))
    if o.get("ia"):
        print(f"  {P.ia_txt(o['ia'])}")
ias = [o for o in dia["operaciones"] if (o.get("ia") or {}).get("decision")]
if ias or dia.get("vetadas"):
    ap = [o for o in ias if o["ia"]["decision"] == "aprobar"]; ve = [o for o in ias if o["ia"]["decision"] == "vetar"]
    print(f"\n**Segunda opinión de Claude:** aprobó {len(ap)} ({usd(sum(o['pnl'] for o in ap))}) · habría vetado {len(ve)} "
          f"({usd(sum(o['pnl'] for o in ve))}) · si se hubiera hecho caso, el día sería {usd(sum(o['pnl'] for o in ap) + sum(o['pnl'] for o in dia['operaciones'] if not (o.get('ia') or {}).get('decision')))}"
          + (f" · vetadas en modo activo: {len(dia['vetadas'])}" if dia.get("vetadas") else ""))

# 2. régimen cada 15 min
print("\n## Régimen del tablero (cada 15 min, ET)\n\n| ET | SPX | zero γ | γ | tend 5/15/1h | mom | major + | major − |\n|---|---|---|---|---|---|---|---|")
bb = P.Bot(fecha)
for ts, r in filas:
    m = P.mins(ts)
    if 510 <= m <= 900 and m % 15 == 0:
        c = bb.ctx(r)
        print(f"| {P.et(ts)} | {c['px']:.0f} | {c['zg']:.0f} | {'+' if c['g'] > 0 else '−' if c['g'] < 0 else '·'} | {c['tf2']} | {c['mom']} | {c['mp']} | {c['mn']} |")
px = [r["px"] for ts, r in filas if 510 <= P.mins(ts) <= 900]
if px:
    print(f"\nApertura {px[0]:.2f} · máx {max(px):.2f} · mín {min(px):.2f} · último {px[-1]:.2f} · rango {max(px) - min(px):.0f} pts")

# 3. tape
tape = leer_tape()
if tape:
    print(f"\n## Tape: {len(tape)} minutos registrados · edad máx de la foto {max(t.get('edad') or 0 for t in tape)} s")
    for t in tape:
        if t["ts"].endswith(("00", "30")):
            print(f"- {P.et(t['ts'])} ET neto C {t['neto'].get('C') if t.get('neto') else '—'} P {t['neto'].get('P') if t.get('neto') else '—'} · flujo 5 min {t.get('flujo5')}")
else:
    print("\n## Tape: sin registro de hoy")

# 4. real
reales = [o for o in bit.get("ops", []) if o.get("fecha") == fecha and o.get("motor") == "spx0dte"]
if reales:
    tot = sum(o.get("pnl") or 0 for o in reales)
    print(f"\n## Real (Bitácora): {usd(tot)} · {len(reales)} ops · bot − real = {usd(dia['pnl'] - tot)}")
else:
    print("\n## Real (Bitácora): aún sin operaciones registradas para este día")

# 4b. clasificador del tipo de día (fase 1: solo observa)
print("\n## Tipo de día (clasificador, fase 1)")
cl = dia.get("clasif") or P.correr_dia(fecha).resumen().get("clasif") or []
real = dia.get("real") or P.correr_dia(fecha).resumen().get("real")
for c in cl:
    ia = c.get("ia") or {}
    print(f"- {c['hora']} ET · reglas: **{c['reglas']['tipo']}** {c['reglas']['conf']}% ({'; '.join(c['reglas']['razones'])})"
          + (f" · Claude: **{ia.get('tipo')}** {ia.get('direccion', '')} {ia.get('confianza')}% ({ia.get('razon')})" if ia and not ia.get("error") else ""))
print(f"- **Real al cierre:** {real['tipo']} · {real['detalle']}" if real else "- Real al cierre: sin dato")
acc = {h: [0, 0] for h in ("10:00", "11:00", "13:00")}
por_reg = {}
for f in sorted({x.name[:10] for x in carpeta.glob("20??-??-??.jsonl*")}):
    try:
        x = P.correr_dia(f); rs = x.resumen()
    except Exception:
        continue
    rl = (rs.get("real") or {}).get("tipo")
    if not rl:
        continue
    for c in rs.get("clasif", []):
        if c["hora"] in acc:
            acc[c["hora"]][1] += 1; acc[c["hora"]][0] += c["reglas"]["tipo"] == rl
    for o in x.ops:
        k = (rl, o["regla"][:1]); v = por_reg.setdefault(k, [0, 0]); v[0] += o["pnl"]; v[1] += 1
print("\nAcierto acumulado del clasificador de reglas (repetición): " + " · ".join(f"{h} {a}/{n}" for h, (a, n) in acc.items()))
print("\n| Día real | Regla | Ops | P&L |\n|---|---|---|---|")
for (rl, rg), (pl, n) in sorted(por_reg.items()):
    print(f"| {rl} | {rg} | {n} | {usd(pl)} |")

# 5. sensibilidad del Playbook en este mismo día (y acumulado de días con historial)
variantes = {
    "v1 (actual)": {},
    "abre 10:30 ET": {"abre": 9 * 60 + 30},
    "débito TP 35%": {"deb_tp": 0.35}, "débito TP 65%": {"deb_tp": 0.65},
    "débito SL 35%": {"deb_sl": 0.35}, "débito SL 65%": {"deb_sl": 0.65},
    "débito δ0.40": {"deb_delta": 0.40},
    "sin regla B": {"ultima_venta": 0},
    "venta TP 65%": {"ven_tp": 0.65},
    "sin regla C": {"pin": (0, 0)},
}
fechas = sorted({f.name[:10] for f in carpeta.glob("20??-??-??.jsonl*")})
print(f"\n## Sensibilidad del Playbook (repetición) · hoy y acumulado de {len(fechas)} días\n\n| Variante | Hoy | Ops hoy | Acumulado | Acierto acum. |\n|---|---|---|---|---|")
for nom, cambio in variantes.items():
    pb = {**P.PB, **cambio}
    hoy = P.correr_dia(fecha, pb)
    acum, n, g = 0, 0, 0
    for f in fechas:
        try:
            x = P.correr_dia(f, pb)
        except Exception:
            continue
        acum += x.pnl_dia(); n += len(x.ops); g += sum(1 for o in x.ops if o["pnl"] > 0)
    print(f"| {nom} | {usd(hoy.pnl_dia())} | {len(hoy.ops)} | {usd(acum)} | {round(100 * g / n) if n else 0}% ({n}) |")
print("\nNota: la repetición marca con Black-Scholes; en vivo el bot marca con la cadena real de Schwab. "
      "Muestra chica: una variante solo es candidata si mejora el acumulado sin depender de un solo día.")
