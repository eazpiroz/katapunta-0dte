#!/usr/bin/env python3
"""
Katapunta · Bot de papel SPX 0DTE (paper trading, NUNCA manda órdenes).

Opera SOLO en papel con las mismas lecturas que ve el tablero (el historial por minuto que escribe
flightdeck_recolector.py) y el Playbook v1 escrito abajo. Sirve para validar el tablero y el Playbook:
cada operación guarda la regla que la disparó y el contexto (precio, zero γ, majors, tendencia).

Seguridad: el único acceso a Schwab es GET a Market Data (cadena de opciones para marcar posiciones).
No existe código de cuentas ni de órdenes; `market_get` rechaza cualquier otra URL.

Uso (en la carpeta del recolector, con el recolector corriendo):
    python papel_bot.py                     # en vivo: sigue el historial de hoy, marca con Schwab, publica papel.json
    python papel_bot.py --replay 2026-11-12 # repite un día del historial (marca con Black-Scholes)
    python papel_bot.py --replay todo       # todos los días del historial
    python papel_bot.py --replay todo --local   # sin publicar en GitHub
Solo Python 3.9+, sin librerías externas.
"""
import base64, datetime as dt, glob, gzip, json, math, os, sys, time, traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = Path(__file__).resolve().parent
HIST = HERE / "historial"
STATE = HERE / "papel_estado.json"
OUT = HERE / "papel.json"
MARKET = "https://api.schwabapi.com/marketdata/v1"

# ------------------------------------------------------------------ Playbook v1 (todo editable aquí)
PB = {
    "version": "v1 · 15-nov-2026",
    "abre": 9 * 60,            # CDMX: no abrir antes de 9:00 (10:00 ET)
    "ultima_tend": 13 * 60 + 30,   # último débito de tendencia 13:30 (14:30 ET)
    "ultima_venta": 14 * 60,       # última venta anclada 14:00 (15:00 ET)
    "pin": (14 * 60, 14 * 60 + 30),  # ventana del IC de pin 14:00–14:30 (15:00–15:30 ET)
    "cierre": 15 * 60,         # liquidación
    "gtol": 3.0,               # banda del zero γ para llamar positiva / negativa
    "riesgo": 1000.0,          # $ máximos en riesgo por operación
    "max_ctr": 5,
    "max_ops": 5,              # entradas por día
    "stop_dia": -1500.0,       # si el día llega aquí, no abre más
    "enfriar": 15,             # min sin abrir después de una pérdida
    "contra": 10,              # min antes de abrir en la dirección contraria a la última cerrada
    "slip": 0.05,              # deslizamiento por spread (sobre el mid) al entrar y al salir
    "deb_delta": 0.30, "deb_ancho": 10, "deb_max": 0.60,   # débito: δ del largo, ancho, débito máx (fracción del ancho)
    "deb_tp": 0.50,            # TP débito: capturar 50% de la ganancia máxima
    "deb_sl": 0.50,            # SL débito: el spread pierde 50% del débito
    "ven_ancho": 10, "ven_dmax": 0.20, "ven_min": 0.15,    # venta: ancho, δ máx del corto, crédito mín (fracción del ancho)
    "ven_tp": 0.50,            # TP venta: recomprar al 50% del crédito
    "ven_cat": 2.5,            # stop de catástrofe: el spread vale 2.5× el crédito
    "nivel_buf": 3.0,          # stop por nivel: cierra si el precio pasa el ancla por 3 pts dos minutos seguidos
    "pin_dist": 10.0,          # IC de pin: precio a ≤10 pts del major +
}


def mins(ts):
    h, m = ts.split(":")[:2]
    return int(h) * 60 + int(m)


def et(ts):
    """CDMX → ET (+1 h en noviembre-marzo)."""
    m = mins(ts) + 60
    return f"{m // 60}:{m % 60:02d}"


# ------------------------------------------------------------------ valuación
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, side):
    if T <= 0 or iv <= 0:
        return max(0.0, S - K) if side == "c" else max(0.0, K - S)
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / sq
    d2 = d1 - sq
    return S * ncdf(d1) - K * ncdf(d2) if side == "c" else K * ncdf(-d2) - S * ncdf(-d1)


def tyears(ts, cierre):
    return max(0.0, (cierre - mins(ts)) / (365 * 1440))


def iv_de(r, ts, cierre):
    """IV implícita del straddle restante (rem = precio ± straddle)."""
    T = tyears(ts, cierre)
    rem = r.get("rem") or [r["px"], r["px"]]
    half = max(0.5, (rem[1] - rem[0]) / 2)
    return half / (0.7979 * r["px"] * math.sqrt(T)) if T > 0 else 0.0


def valor_bs(legs, S, ts, iv, cierre):
    T = tyears(ts, cierre)
    return sum(q * bs(S, K, T, iv, sd) for sd, K, q in legs)


def delta_bs(S, K, T, iv, side):
    if T <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return ncdf(d1) if side == "c" else ncdf(d1) - 1


def lad_bs(r, ts, cierre):
    """Escalera estimada con Black-Scholes para días del historial sin cotizaciones (antes del 5-nov)."""
    S, T, iv = r["px"], tyears(ts, cierre), iv_de(r, ts, cierre)
    if T <= 0 or iv <= 0:
        return {}
    def k_delta(d, sd):
        ks = [S + i * 5 * (1 if sd == "c" else -1) for i in range(0, 80)]
        ks = [round(k / 5) * 5 for k in ks]
        return min(ks, key=lambda k: abs(abs(delta_bs(S, k, T, iv, sd)) - d))
    out = {"v": [], "c": []}
    for sd in ("c", "p"):
        sg = 1 if sd == "c" else -1
        for d in (0.05, 0.10, 0.15, 0.20, 0.25):
            k = k_delta(d, sd)
            for w in (5, 10):
                v = bs(S, k, T, iv, sd) - bs(S, k + sg * w, T, iv, sd)
                out["v"].append([sd, d, k, k + sg * w, round(v, 2), round(v, 2)])
        for d in (0.40, 0.30, 0.20, 0.15):
            k = k_delta(d, sd)
            for w in (10, 20):
                v = bs(S, k, T, iv, sd) - bs(S, k + sg * w, T, iv, sd)
                out["c"].append([sd, d, k, k + sg * w, round(v, 2), round(v, 2)])
    return out


def intrinseco(legs, S):
    return sum(q * (max(0.0, S - K) if sd == "c" else max(0.0, K - S)) for sd, K, q in legs)


# ------------------------------------------------------------------ Schwab (solo Market Data)
class Marcador:
    """Marca posiciones abiertas con la cadena 0DTE real de Schwab (mid). Solo GET a Market Data."""

    def __init__(self):
        import flightdeck_recolector as R
        self.R = R

    def market_get(self, path, **params):
        import urllib.parse, urllib.request
        url = MARKET + path + "?" + urllib.parse.urlencode(params)
        assert url.startswith(MARKET + "/chains") or url.startswith(MARKET + "/quotes"), "solo Market Data"
        c = self.R.jload(self.R.SCHWAB)          # relee: así usa el token que renovó el recolector
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.R.access(c), "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())

    def cadena(self, hoy):
        j = self.market_get("/chains", symbol="$SPX", contractType="ALL", strikeCount=80, fromDate=hoy, toDate=hoy)
        m = {}
        for sd, key in (("c", "callExpDateMap"), ("p", "putExpDateMap")):
            for _, ks in j.get(key, {}).items():
                for k, lst in ks.items():
                    o = lst[0]
                    if str(o.get("symbol", "")).startswith("SPXW"):
                        m[(sd, float(k))] = ((o.get("bid") or 0) + (o.get("ask") or 0)) / 2
        return m


# ------------------------------------------------------------------ el bot
class Bot:
    def __init__(self, fecha, pb=PB):
        self.f, self.pb = fecha, pb
        self.abiertas, self.ops = [], []
        self.ult_cierre = {}            # dirección → minuto del último cierre
        self.ult_perdida = -999
        self.n_ent = 0
        self.prev = None
        self.brk = {}                   # id → minutos seguidos con el ancla rota
        self.sig = 0
        self.nota = None

    # -- contexto ------------------------------------------------------
    def ctx(self, r):
        px, zg = r["px"], r.get("zg") or r["px"]
        g = 1 if px > zg + self.pb["gtol"] else -1 if px < zg - self.pb["gtol"] else 0
        tf2 = r.get("tf2") or [0, 0, 0]
        T = tf2[0] if tf2[0] == tf2[1] else 0
        mom = (r["mom"] or [0])[0] if "mom" in r else (tf2[0] if tf2[0] == tf2[1] else 0)   # días sin momentum: usa la tendencia
        return {"px": px, "zg": round(zg, 1), "g": g, "T": T, "mom": mom, "mp": r.get("mp"), "mn": r.get("mn"),
                "techo": (r.get("techo") or [None])[0], "piso": (r.get("piso") or [None])[0], "tf2": tf2,
                "cw": (r.get("cw") or [None])[0], "pw": (r.get("pw") or [None])[0]}

    def pnl_dia(self):
        return sum(o["pnl"] for o in self.ops)

    def puede(self, m, dire):
        pb = self.pb
        if self.n_ent >= pb["max_ops"] or self.pnl_dia() <= pb["stop_dia"]:
            return False
        if m - self.ult_perdida < pb["enfriar"]:
            return False
        if m - self.ult_cierre.get(-dire, -999) < pb["contra"]:
            return False
        if any(p["dir"] == dire or p["dir"] == -dire and p["tipo"] != "venta" for p in self.abiertas):
            return False
        return True

    def ctr(self, riesgo_spread):
        return max(1, min(self.pb["max_ctr"], int(self.pb["riesgo"] // max(1.0, riesgo_spread * 100))))

    # -- entradas ------------------------------------------------------
    def abrir(self, ts, r, c, tipo, dire, legs, precio, ancho, regla, ancla=None, objetivo=None):
        credito = precio < 0
        riesgo = ancho + precio if credito else precio          # precio <0 = crédito
        n = self.ctr(riesgo)
        p = {"id": f"{self.f}-{len(self.ops) + len(self.abiertas) + 1}", "tipo": tipo, "dir": dire, "legs": legs,
             "entrada": round(precio, 2), "n": n, "ancho": ancho, "regla": regla, "ancla": ancla, "obj": objetivo,
             "t_in": ts[:5], "estimado": bool(r.get("_bs")), "ctx": {k: c[k] for k in ("px", "zg", "g", "T", "mp", "mn", "tf2")}, "max": 0.0, "min": 0.0}
        iv = iv_de(r, ts, self.pb["cierre"])
        p["ajuste"] = precio - valor_bs(legs, r["px"], ts, iv, self.pb["cierre"])   # skew/spread real vs BS al entrar
        p["m_in"] = mins(ts)
        self.abiertas.append(p)
        self.n_ent += 1

    def estructura(self, p):
        ks = sorted({K for _, K, _ in p["legs"]})
        nom = {"tend": "Débito", "venta": "Crédito", "pin": "IC de pin"}[p["tipo"]]
        if p["tipo"] == "pin":
            return f"Iron condor {ks[0]:.0f}/{ks[1]:.0f} – {ks[2]:.0f}/{ks[3]:.0f} ×{p['n']}"
        sd = p["legs"][0][0]
        lado = "Call" if sd == "c" else "Put"
        hecho = "comprado" if p["tipo"] == "tend" else "vendido"
        larg = [K for _, K, q in p["legs"] if q > 0][0]
        cort = [K for _, K, q in p["legs"] if q < 0][0]
        a, b = (larg, cort) if p["tipo"] == "tend" else (cort, larg)
        return f"{lado} spread {hecho} {a:.0f}/{b:.0f} ×{p['n']}"

    def buscar(self, ts, r, c):
        m = mins(ts)
        pb = self.pb
        if m < pb["abre"]:
            return
        lad = r.get("lad") or lad_bs(r, ts, pb["cierre"])
        # A · gamma negativa + tendencia alineada → débito a favor
        if c["g"] == -1 and c["T"] != 0 and c["mom"] == c["T"] and m <= pb["ultima_tend"] and self.puede(m, c["T"]):
            sd = "c" if c["T"] > 0 else "p"
            cand = [x for x in lad.get("c", []) if x[0] == sd and abs(x[1] - pb["deb_delta"]) < 1e-6
                    and abs(x[3] - x[2]) == pb["deb_ancho"] and 0 < x[5] <= pb["deb_max"] * pb["deb_ancho"]]
            if cand:
                x = cand[0]
                deb = x[5] + pb["slip"]
                if c["T"] < 0:
                    obj = c["mn"] if c["mn"] and c["mn"] < c["px"] - 5 else c["piso"]
                else:
                    obj = c["mp"] if c["mp"] and c["mp"] > c["px"] + 5 else c["techo"]
                self.abrir(ts, r, c, "tend", c["T"], [(sd, x[2], 1), (sd, x[3], -1)], deb, pb["deb_ancho"],
                           f"A · gamma negativa ({c['px']:.0f} < zero γ {c['zg']:.0f}) y tendencia "
                           f"{'alcista' if c['T'] > 0 else 'bajista'} en 5 y 15 min con momentum → débito a favor; objetivo {obj}",
                           objetivo=obj)
                return
        # B · gamma positiva → venta anclada a un nivel (soporte abajo / resistencia arriba)
        if c["g"] == 1 and m <= pb["ultima_venta"]:
            for dire in ([1, -1] if c["T"] == 0 else [c["T"]]):
                if not self.puede(m, dire) or any(p["tipo"] == "venta" and p["dir"] == dire for p in self.abiertas):
                    continue
                if dire > 0:     # bull put debajo del soporte
                    niv = [v for v in (c["zg"], c["mn"], c["piso"], c["pw"]) if v and v < c["px"] - 10]
                    if not niv:
                        continue
                    ancla = max(niv); sd = "p"; ok = lambda K: K <= ancla - 5
                else:            # bear call arriba de la resistencia
                    niv = [v for v in (c["mp"], c["techo"], c["cw"]) if v and v > c["px"] + 10]
                    if not niv:
                        continue
                    ancla = min(niv); sd = "c"; ok = lambda K: K >= ancla + 5
                cand = [x for x in lad.get("v", []) if x[0] == sd and x[1] <= pb["ven_dmax"] + 1e-9
                        and abs(x[3] - x[2]) == pb["ven_ancho"] and ok(x[2]) and x[5] >= pb["ven_min"] * pb["ven_ancho"]]
                if cand:
                    x = max(cand, key=lambda y: y[1])
                    cred = x[5] - pb["slip"]
                    # el stop es el nivel más cercano que protege al corto (no el primero que toque el precio)
                    prot = [v for v in niv if (v >= x[2] + 5 if dire > 0 else v <= x[2] - 5)]
                    ancla = min(prot) if dire > 0 else max(prot)
                    self.abrir(ts, r, c, "venta", dire, [(sd, x[2], -1), (sd, x[3], 1)], -cred, pb["ven_ancho"],
                               f"B · gamma positiva ({c['px']:.0f} > zero γ {c['zg']:.0f}) → vender "
                               f"{'debajo del soporte' if dire > 0 else 'arriba de la resistencia'} {ancla:.0f} "
                               f"(corto {x[2]:.0f}, δ {x[1]:.2f}); stop solo si se rompe {ancla:.0f}", ancla=ancla)
                    return
        # C · IC de pin al final del día
        a, b = pb["pin"]
        if a <= m <= b and c["g"] == 1 and c["mp"] and abs(c["px"] - c["mp"]) <= pb["pin_dist"] \
                and not any(p["tipo"] == "pin" for p in self.abiertas) and self.n_ent < pb["max_ops"] \
                and self.pnl_dia() > pb["stop_dia"]:
            def pick(sd):
                cs = [x for x in lad.get("v", []) if x[0] == sd and abs(x[1] - 0.20) < 1e-6 and abs(x[3] - x[2]) == 5]
                return cs[0] if cs else None
            xc, xp = pick("c"), pick("p")
            if xc and xp and xc[5] + xp[5] > 0.5:
                cred = xc[5] + xp[5] - pb["slip"]
                legs = [("p", xp[3], 1), ("p", xp[2], -1), ("c", xc[2], -1), ("c", xc[3], 1)]
                self.abrir(ts, r, c, "pin", 0, legs, -cred, 5,
                           f"C · pin: gamma positiva y precio a {abs(c['px'] - c['mp']):.0f} pts del major + {c['mp']:.0f} → "
                           f"IC δ0.20 al vencimiento; stop solo si sale del corto")

    # -- gestión -------------------------------------------------------
    def valor(self, p, ts, r, cad):
        """Valor del spread (positivo = vale a favor de un largo). Real (cadena) o Black-Scholes con ajuste."""
        if cad:
            try:
                return sum(q * cad[(sd, K)] for sd, K, q in p["legs"])
            except KeyError:
                pass
        iv = iv_de(r, ts, self.pb["cierre"])
        frac = max(0.0, (self.pb["cierre"] - mins(ts)) / max(1, self.pb["cierre"] - p["m_in"]))
        return valor_bs(p["legs"], r["px"], ts, iv, self.pb["cierre"]) + p["ajuste"] * frac

    def cerrar(self, p, ts, precio, motivo):
        pnl = round((precio - p["entrada"]) * 100 * p["n"], 0)
        self.abiertas.remove(p)
        m = mins(ts)
        self.ult_cierre[p["dir"]] = m
        if pnl < 0:
            self.ult_perdida = m
        self.ops.append({"id": p["id"], "fecha": self.f, "estructura": self.estructura(p), "tipo": p["tipo"],
                         "entra": et(p["t_in"]), "sale": et(ts), "entrada": p["entrada"], "salida": round(precio, 2),
                         "n": p["n"], "pnl": pnl, "regla": p["regla"], "estimado": p.get("estimado", False), "motivo": motivo, "ctx": p["ctx"],
                         "mejor": round((p["max"]) * 100 * p["n"], 0), "peor": round(p["min"] * 100 * p["n"], 0)})

    def gestionar(self, ts, r, c, cad):
        pb, m = self.pb, mins(ts)
        for p in list(self.abiertas):
            v = self.valor(p, ts, r, cad)
            g = v - p["entrada"]
            p["max"], p["min"] = max(p["max"], g), min(p["min"], g)
            if p["tipo"] == "tend":
                deb = p["entrada"]
                if v >= deb + pb["deb_tp"] * (p["ancho"] - deb):
                    self.cerrar(p, ts, v - pb["slip"], f"TP: capturó {pb['deb_tp']:.0%} de la ganancia máxima"); continue
                if p["obj"] and ((p["dir"] > 0 and c["px"] >= p["obj"]) or (p["dir"] < 0 and c["px"] <= p["obj"])):
                    self.cerrar(p, ts, v - pb["slip"], f"TP: el precio llegó al nivel objetivo {p['obj']}"); continue
                if v <= deb * (1 - pb["deb_sl"]):
                    self.cerrar(p, ts, v - pb["slip"], f"SL: el spread perdió {pb['deb_sl']:.0%} del débito"); continue
                if c["tf2"][0] == -p["dir"] and c["tf2"][1] == -p["dir"]:
                    self.cerrar(p, ts, v - pb["slip"], "SL: la tendencia de 5 y 15 min se volteó"); continue
            else:
                cred = -p["entrada"]
                if p["tipo"] == "venta" and -v <= cred * (1 - pb["ven_tp"]):
                    self.cerrar(p, ts, v - pb["slip"], f"TP: recompra al {1 - pb['ven_tp']:.0%} del crédito"); continue
                if -v >= cred * pb["ven_cat"]:
                    self.cerrar(p, ts, v - pb["slip"], f"Stop de catástrofe: el spread vale {pb['ven_cat']}× el crédito"); continue
                # stop por nivel: el ancla (venta) o el corto (pin) roto 2 minutos seguidos
                if p["tipo"] == "venta":
                    roto = (c["px"] < p["ancla"] - pb["nivel_buf"]) if p["dir"] > 0 else (c["px"] > p["ancla"] + pb["nivel_buf"])
                    lim = p["ancla"]
                else:
                    cortos = sorted(K for _, K, q in p["legs"] if q < 0)
                    roto = c["px"] < cortos[0] - pb["nivel_buf"] or c["px"] > cortos[1] + pb["nivel_buf"]
                    lim = f"{cortos[0]:.0f}/{cortos[1]:.0f}"
                self.brk[p["id"]] = self.brk.get(p["id"], 0) + 1 if roto else 0
                if self.brk[p["id"]] >= 2:
                    self.cerrar(p, ts, v - pb["slip"], f"Stop por nivel: el precio rompió {lim} dos minutos seguidos"); continue

    def liquidar(self, ts, px):
        for p in list(self.abiertas):
            self.cerrar(p, ts, intrinseco(p["legs"], px), f"Vencimiento: SPX liquidó en {px:.2f}")

    def paso(self, ts, r, cad=None):
        c = self.ctx(r)
        if mins(ts) >= self.pb["cierre"] - 1:
            self.liquidar(ts, c["px"])
            return
        self.gestionar(ts, r, c, cad)
        self.buscar(ts, r, c)

    def resumen(self):
        n = len(self.ops)
        g = sum(1 for o in self.ops if o["pnl"] > 0)
        return {"fecha": self.f, "pnl": self.pnl_dia(), "ops": n, "ganadoras": g, "nota": self.nota,
                "abiertas": [{"estructura": self.estructura(p), "entra": et(p["t_in"]), "entrada": p["entrada"], "regla": p["regla"]}
                             for p in self.abiertas]}


# ------------------------------------------------------------------ datos
def leer_dia(fecha):
    """Lecturas SPX por minuto del historial (local .jsonl, .jsonl.gz o rama live de GitHub)."""
    for f in (HIST / f"{fecha}.jsonl", HIST / f"{fecha}.jsonl.gz"):
        if f.exists():
            txt = gzip.decompress(f.read_bytes()).decode() if f.suffix == ".gz" else f.read_text(encoding="utf-8")
            break
    else:
        import urllib.request
        url = f"https://raw.githubusercontent.com/eazpiroz/katapunta-0dte/live/historial/{fecha}.jsonl.gz"
        with urllib.request.urlopen(url, timeout=30) as r:
            txt = gzip.decompress(r.read()).decode()
    out = []
    for ln in txt.splitlines():
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if j.get("SPX", {}).get("px"):
            j["SPX"]["_bs"] = "lad" not in j["SPX"]
            out.append((j["ts"], j["SPX"]))
    return out


def correr_dia(fecha):
    b = Bot(fecha)
    filas = leer_dia(fecha)
    for ts, r in filas:
        if mins(ts) < 8 * 60 + 30:
            continue
        b.paso(ts, r)
        if mins(ts) >= PB["cierre"] - 1:
            break
    liq = json.loads((HERE / "liquidaciones.json").read_text()) if (HERE / "liquidaciones.json").exists() else {}
    if filas and mins(filas[-1][0]) < PB["cierre"] - 5:
        b.nota = f"El historial termina a las {et(filas[-1][0])} ET (el recolector se detuvo): sin lecturas después de esa hora."
    if b.abiertas and filas:                 # el historial terminó antes de las 14:59
        ts, r = filas[-1]
        b.liquidar("14:59", liq.get(fecha, r["px"]))
    return b


# ------------------------------------------------------------------ publicación
def armar(dias_bots, modo):
    prev = json.loads(OUT.read_text()) if OUT.exists() else {}
    dias = prev.get("dias", {})
    for b in dias_bots:
        dias[b.f] = {**b.resumen(), "modo": modo, "operaciones": b.ops}
    return {"actualizado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "modo": modo,
            "playbook": {k: v for k, v in PB.items()}, "dias": dict(sorted(dias.items()))}


def publicar(data):
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--local" in sys.argv:
        return
    try:
        import flightdeck_recolector as R
        tok = R.gh_token()
        path = f"/repos/{R.REPO}/contents/papel.json"
        st, j = R.gh_req(tok, "GET", path + f"?ref={R.BRANCH}")
        body = {"message": "papel " + dt.datetime.now().strftime("%H:%M"), "branch": R.BRANCH,
                "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()}
        if st == 200 and j.get("sha"):
            body["sha"] = j["sha"]
        st, j = R.gh_req(tok, "PUT", path, body)
        if st not in (200, 201):
            print("GitHub:", st, j.get("message"))
    except Exception as e:
        print("publicar:", e)


def imprimir(b):
    print(f"\n{b.f}  P&L ${b.pnl_dia():,.0f}  ({len(b.ops)} ops)" + (f"  ⚠ {b.nota}" if b.nota else ""))
    for o in b.ops:
        print(f"  {o['entra']}→{o['sale']} ET  {o['estructura']:<40} {o['entrada']:>6} → {o['salida']:>6}  {o['pnl']:>+7,.0f}  | {o['motivo']}")


def traer_remoto():
    """Copia local del papel.json publicado, para no perder los días anteriores al publicar."""
    try:
        import flightdeck_recolector as R
        st, j = R.gh_req(R.gh_token(), "GET", f"/repos/{R.REPO}/contents/papel.json?ref={R.BRANCH}")
        if st == 200:
            OUT.write_bytes(base64.b64decode(j["content"]))
    except Exception as e:
        print("no pude traer papel.json:", e)


def vivo():
    traer_remoto()
    hoy = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).date().isoformat()
    est = json.loads(STATE.read_text()) if STATE.exists() else {}
    b = Bot(hoy)
    mk = Marcador()
    hecho = 0
    if est.get("fecha") == hoy:          # reinicio a media sesión: re-procesa el historial de hoy sin marcas reales
        print("Reanudando: vuelvo a pasar el historial de hoy.")
    print(f"Bot de papel en marcha ({PB['version']}). Solo papel: no existe código de órdenes. Ctrl+C para salir.\n")
    sin_datos = 0
    while True:
        f = HIST / f"{hoy}.jsonl"
        filas = []
        if f.exists():
            for ln in f.read_text(encoding="utf-8").splitlines()[hecho:]:
                try:
                    j = json.loads(ln)
                    if j.get("SPX", {}).get("px"):
                        filas.append((j["ts"], j["SPX"]))
                except ValueError:
                    pass
                hecho += 1
        if not filas:
            sin_datos += 1
            if sin_datos == 6:
                print("⚠ 3 min sin lecturas nuevas: ¿está corriendo el recolector?")
        else:
            sin_datos = 0
        for i, (ts, r) in enumerate(filas):
            if mins(ts) < 8 * 60 + 30:
                continue
            cad = None
            if b.abiertas and i == len(filas) - 1:
                try:
                    cad = mk.cadena(hoy)
                except Exception as e:
                    print("  cadena Schwab:", e, "→ marco con Black-Scholes")
            antes = len(b.ops) + len(b.abiertas)
            b.paso(ts, r, cad)
            if len(b.ops) + len(b.abiertas) != antes or i == len(filas) - 1:
                print(f"{et(ts)} ET  SPX {r['px']}  abiertas {len(b.abiertas)}  cerradas {len(b.ops)}  P&L ${b.pnl_dia():,.0f}")
        if filas:
            STATE.write_text(json.dumps({"fecha": hoy}))
            publicar(armar([b], "vivo"))
        t = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6)))
        if t.hour * 60 + t.minute >= PB["cierre"] + 2:
            if b.abiertas and filas:
                b.liquidar(*[(filas[-1][0], filas[-1][1]["px"])][0])
            publicar(armar([b], "vivo"))
            imprimir(b)
            print("\nFin del día.")
            break
        time.sleep(20)


def main():
    if "--replay" in sys.argv:
        q = sys.argv[sys.argv.index("--replay") + 1]
        if q == "todo":
            fechas = sorted({Path(x).name[:10] for x in glob.glob(str(HIST / "*.jsonl*"))})
        else:
            fechas = [q]
        if "--local" not in sys.argv:
            traer_remoto()
        bots = []
        for f in fechas:
            try:
                b = correr_dia(f)
            except Exception as e:
                print(f, "sin historial:", e)
                continue
            imprimir(b)
            bots.append(b)
        tot = sum(b.pnl_dia() for b in bots)
        n = sum(len(b.ops) for b in bots)
        g = sum(1 for b in bots for o in b.ops if o["pnl"] > 0)
        print(f"\nTOTAL {len(bots)} días · {n} operaciones · {g} ganadoras · P&L ${tot:,.0f}")
        publicar(armar(bots, "replay"))
        return
    vivo()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido.")
    except Exception:
        traceback.print_exc()
