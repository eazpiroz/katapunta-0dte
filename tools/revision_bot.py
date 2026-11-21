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
    "A: recorrido ≥10": {"a_recorrido": 10},
    "A: recorrido ≥15": {"a_recorrido": 15},
    "A: objetivo pasa el largo": {"obj_pasa_largo": True},
    "B: apagada en tendencia": {"b_sin_tendencia": True},
    "65% + recorrido ≥15": {"deb_tp": 0.65, "a_recorrido": 15},
}
fechas = sorted({f.name[:10] for f in carpeta.glob("20??-??-??.jsonl*")})
por_dia = {}                                   # variante → {fecha: (pnl, ops, ganadoras)}
for nom, cambio in variantes.items():
    pb = {**P.PB, **cambio}
    for f in fechas:
        try:
            x = P.correr_dia(f, pb)
        except Exception:
            continue
        por_dia.setdefault(nom, {})[f] = (x.pnl_dia(), len(x.ops), sum(1 for o in x.ops if o["pnl"] > 0))
print(f"\n## Sensibilidad del Playbook (repetición) · hoy y acumulado de {len(fechas)} días\n\n| Variante | Hoy | Ops hoy | Acumulado | Acierto acum. | Días mejor / peor vs v1 |\n|---|---|---|---|---|---|")
base = por_dia.get("v1 (actual)", {})
seguimiento = {}
for nom in variantes:
    d = por_dia.get(nom, {})
    hoy = d.get(fecha, (0, 0, 0))
    acum, n, g = (sum(v[i] for v in d.values()) for i in range(3))
    mejor = sum(1 for f, v in d.items() if f in base and v[0] > base[f][0] + 0.5)
    peor = sum(1 for f, v in d.items() if f in base and v[0] < base[f][0] - 0.5)
    seguimiento[nom] = (mejor, peor, len(d))
    mp = "—" if nom == "v1 (actual)" else f"{mejor} / {peor}"
    print(f"| {nom} | {usd(hoy[0])} | {hoy[1]} | {usd(acum)} | {round(100 * g / n) if n else 0}% ({n}) | {mp} |")

# desglose por día (para ver si una mejora depende de un solo día)
cols = list(variantes)
print("\n### Desglose por día (repetición, P&L)\n\n| Día | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols))
for f in fechas:
    print(f"| {f[5:]} | " + " | ".join(usd(por_dia.get(c, {}).get(f, (0,))[0]) for c in cols) + " |")

# seguimiento: criterio para proponer adoptar una variante
MIN_DIAS, MIN_MEJOR = 15, 4
print(f"\n### Seguimiento de candidatas (criterio: ≥{MIN_DIAS} días, 0 días peor, ≥{MIN_MEJOR} días mejor)\n")
for nom, (mejor, peor, nd) in seguimiento.items():
    if nom == "v1 (actual)" or mejor == 0:
        continue
    if peor:
        est = "descartada por ahora (empeora algún día)"
    elif nd >= MIN_DIAS and mejor >= MIN_MEJOR:
        est = "**cumple el criterio: lista para proponer**"
    else:
        est = f"en observación (faltan {max(0, MIN_DIAS - nd)} días y {max(0, MIN_MEJOR - mejor)} días mejores)"
    print(f"- {nom}: {mejor} mejor / {peor} peor en {nd} días → {est}")

# 6. filtro de tape: no abrir si el flujo direccional de 5 min va en contra (solo días con tape registrado)
def tape_dia(f):
    for t in (carpeta / f"tape_min_{f}.jsonl", carpeta / f"tape_min_{f}.jsonl.gz"):
        if t.exists():
            txt = gzip.decompress(t.read_bytes()).decode() if t.suffix == ".gz" else t.read_text(encoding="utf-8")
            return {j["ts"][:5]: j for j in (json.loads(x) for x in txt.splitlines() if x.strip())}
    return {}


class BotTape(P.Bot):
    """Igual que el bot, pero con el tape del minuto y (si filtra) sin entradas contra el flujo direccional:
    (flujo5 calls − flujo5 puts) × dirección < 0. Sin foto de tape ese minuto → no filtra."""
    filtrar, cinta, bloqueadas = False, {}, 0

    def paso(self, ts, r):
        self.tape = self.cinta.get(ts[:5]) or self.tape
        return super().paso(ts, r)

    def abrir(self, ts, r, c, tipo, dire, *a, **k):
        fl = (self.tape or {}).get("flujo5") or {}
        if self.filtrar and dire and fl and (fl.get("C", 0) - fl.get("P", 0)) * dire < 0:
            self.bloqueadas += 1
            return
        return super().abrir(ts, r, c, tipo, dire, *a, **k)


def correr_tape(f, filtrar):
    b = BotTape(f, P.PB); b.filtrar, b.cinta, b.bloqueadas = filtrar, tape_dia(f), 0
    filas = P.leer_dia(f)
    for ts, r in filas:
        if P.mins(ts) < 8 * 60 + 30:
            continue
        b.paso(ts, r)
        if P.mins(ts) >= P.PB["cierre"] - 1:
            break
    if b.abiertas and filas:
        b.liquidar("14:59", filas[-1][1]["px"])
    return b


con_tape = [f for f in fechas if tape_dia(f)]
print(f"\n## Filtro de tape (repetición) · {len(con_tape)} días con tape\n")
if con_tape:
    print("| Día | v1 con tape | Con filtro | Diferencia | Intentos bloqueados (min) |\n|---|---|---|---|---|")
    tv = tf = 0
    for f in con_tape:
        try:
            a, z = correr_tape(f, False), correr_tape(f, True)
        except Exception as e:
            print(f"| {f[5:]} | error {e.__class__.__name__} | | | |"); continue
        tv += a.pnl_dia(); tf += z.pnl_dia()
        print(f"| {f[5:]} | {usd(a.pnl_dia())} | {usd(z.pnl_dia())} | {usd(z.pnl_dia() - a.pnl_dia())} | {z.bloqueadas} |")
    print(f"| **Total** | {usd(tv)} | {usd(tf)} | {usd(tf - tv)} | |")
    print(f"\nCriterio: adoptar solo si mejora el acumulado con ≥10 días de tape (hoy: {len(con_tape)}).")
else:
    print("Sin días con tape registrado todavía.")
# 7. Tape v2: ¿el flujo grande anticipa al SPX? (todos los días con tape en la carpeta)
import tape_estudio as TE
print(f"\n## Reacción del precio al tape (acumulado de todos los días con tape)\n")
TE.main(carpeta)
hoy_t = leer_tape()
if hoy_t and any("liq" in t for t in hoy_t):
    lq = [t["liq"] for t in hoy_t if t.get("liq") and t["liq"].get("x")]
    peor = max(lq, key=lambda x: x["x"]) if lq else None
    bl = sum(len(t.get("bloques_nuevos") or []) for t in hoy_t)
    print(f"\nHoy: {bl} bloques acumulados · liquidez: spread {'máx ' + str(peor['x']) + '× la base' if peor else 'sin base'}"
          f" · volumen > OI al cierre: {', '.join(str(int(a['k'])) + a['t'] for a in (hoy_t[-1].get('apertura') or [])) or '—'}")

print("\nNota: la repetición marca con Black-Scholes; en vivo el bot marca con la cadena real de Schwab. "
      "Muestra chica: una variante solo es candidata si mejora el acumulado sin depender de un solo día.")
