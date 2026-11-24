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
    "capital": 3500.0,         # capital del bot: el riesgo abierto (buying power en uso) nunca pasa de aquí
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
    "gamma_edad": 30,
    # --- candidatas en prueba (apagadas en v1; solo las usa el probador de variantes)
    "a_recorrido": 0,          # A: no comprar si el objetivo está a menos de N pts (0 = apagado)
    "obj_pasa_largo": False,   # A: el TP por nivel solo cuenta si el objetivo pasa el strike largo
    "b_sin_tendencia": False,  # B: no vender si el clasificador (reglas) dice tendencia
    "gamma_propia": False,     # sin lectura γ manual (o con más de gamma_edad min): usar el régimen del GEX propio (cadena de Schwab)
    "c_quietud": 0,            # C ampliada: con SPX a ≤10 pts del PIN del tablero y rango de 30 min ≤ N pts, IC sin exigir gamma + (0 = apagado)          # min: con la lectura γ (perfil manual) más vieja que esto, no abre B ni C (A sí, va por tendencia)
}

# Torneo de bots (23-nov-2026): el PB de arriba es el CONTROL congelado (v1). Este es el carril propio de Claude.
# Solo cambia los viernes, con aprobación del socio. Mismos límites de riesgo que el control.
PB_CLAUDE = {**PB,
    "version": "Claude C1 · 24-nov-2026",
    "deb_tp": 0.65,            # A: TP al 65% de la ganancia máxima (v1: 50%)
    "a_recorrido": 15,         # A: no comprar si al objetivo le quedan menos de 15 pts
    "ven_min": 0.155,          # B: medio ≥ 1.55, para que el crédito cobrado (−0.05) sea ≥ 1.50
}

EPS = 1e-9   # tolerancia de los umbrales: el −50% exacto sí dispara (corrección del 19-nov, aplica a todos los carriles)


# Fase 0 (22-nov): solo medición, no cambia ninguna decisión del bot.
COM_PATA = 1.22    # $ por contrato por pata (comisión + cargos), mediana de tus 78 operaciones SPX 0DTE reales en bitacora_auto


def mins(ts):
    h, m = ts.split(":")[:2]
    return int(h) * 60 + int(m)


def et(ts):
    """CDMX → ET (+1 h en noviembre-marzo)."""
    m = mins(ts) + 60
    return f"{m // 60}:{m % 60:02d}"


# ------------------------------------------------------------------ frescura de la lectura γ
def zg_valido(r):
    """zero γ real: None si el tablero no tiene lectura (el recolector pone zg = precio cuando no sabe)."""
    z = r.get("zg")
    return None if z is None or abs(z - r["px"]) < 1e-6 else z


def edad_gamma(ts, r):
    """Minutos desde la última lectura manual del perfil γ (r["gt"], hora CDMX); None si es estimación propia."""
    gt = r.get("gt")
    if not gt:
        return None
    try:
        return mins(ts) - mins(gt)
    except (ValueError, AttributeError):
        return None


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


# ------------------------------------------------------------------ alertas al celular (ntfy)
ALERTAS = HERE / "papel_alertas.json"     # {"ntfy": "tema-secreto"}  → app ntfy suscrita a ese tema


def sonar(veces=3):
    """Alarma en la computadora (Windows): suena aunque el celular esté en silencio."""
    if os.name != "nt":
        return
    def _s():
        try:
            import winsound
            for _ in range(veces):
                winsound.Beep(1200, 350); winsound.Beep(900, 350); time.sleep(0.3)
        except Exception:
            pass
    import threading
    threading.Thread(target=_s, daemon=True).start()


def mandar_yo(titulo, texto, tags, prioridad=4):
    """Avisos de operación del bot (arranque, llaves, reanudar): solo a tu tema, no al del grupo."""
    mandar(titulo, texto, tags, prioridad, grupo=False)


def mandar(titulo, texto, tags, prioridad=4, grupo=True):
    if prioridad >= 5:
        sonar()
        print("\n" + "=" * 70 + f"\n🔔 {titulo}\n{texto}\n" + "=" * 70 + "\n")
    try:
        cfg = json.loads(ALERTAS.read_text(encoding="utf-8")) if ALERTAS.exists() else {}
    except Exception as e:
        print("alerta (papel_alertas.json):", e); return
    wa = cfg.get("whatsapp") or {}
    if wa.get("telefono") and wa.get("apikey"):      # WhatsApp vía CallMeBot (llave personal, solo en este archivo)
        try:
            import urllib.parse, urllib.request
            q = urllib.parse.urlencode({"phone": wa["telefono"], "text": f"*{titulo}*\n{texto}", "apikey": wa["apikey"]})
            urllib.request.urlopen("https://api.callmebot.com/whatsapp.php?" + q, timeout=20)
        except Exception as e:
            print("alerta WhatsApp:", e)
    temas = [cfg.get("ntfy")] + (list(cfg.get("ntfy_grupo") or []) if grupo else [])   # ntfy_grupo: temas extra (grupo de estudio)
    import urllib.request
    for tema in [t for t in temas if t]:
        try:
            body = json.dumps({"topic": tema, "title": titulo, "message": texto, "tags": tags,
                               "priority": prioridad, "click": "https://0dte.katapunta.com/papel/"}).encode()
            urllib.request.urlopen(urllib.request.Request(cfg.get("servidor", "https://ntfy.sh"), data=body,
                                   headers={"Content-Type": "application/json"}), timeout=10)
        except Exception as e:
            print(f"alerta ({tema[:16]}…):", e)


MESES = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def patas(p, abrir):
    """En palabras: qué strike se compra y cuál se vende (al abrir; al cerrar se invierte)."""
    n = p["n"]
    out = []
    for sd, K, q in sorted(p["legs"], key=lambda x: (x[0], not ((x[2] > 0) == abrir))):   # por lado, primero la compra
        compra = (q > 0) == abrir
        out.append(f"{'🟢 COMPRAR' if compra else '🔴 VENDER'} {n} {'CALL' if sd == 'c' else 'PUT'} {K:.0f}")
    return "\n".join(out)


def orden_tos(p, fecha, abrir, precio):
    """Orden escrita como la muestra thinkorswim, para capturarla igual en paperMoney."""
    d = dt.date.fromisoformat(fecha)
    venc = f"{d.day} {MESES[d.month - 1]} {d.year % 100}"
    n = p["n"]
    if p["tipo"] == "pin":
        ks = {(sd, q): K for sd, K, q in p["legs"]}
        lado, signo = ("SELL", -n) if abrir else ("BUY", n)
        ks_txt = f"{ks[('c', -1)]:.0f}/{ks[('c', 1)]:.0f}/{ks[('p', -1)]:.0f}/{ks[('p', 1)]:.0f} CALL/PUT"
        return f"{lado} {signo:+d} IRON CONDOR SPX 100 (Weeklys) {venc} {ks_txt} @{abs(precio):.2f} LMT"
    sd = p["legs"][0][0]
    larg = [K for _, K, q in p["legs"] if q > 0][0]
    cort = [K for _, K, q in p["legs"] if q < 0][0]
    compra = (p["tipo"] == "tend") == abrir           # abrir débito o cerrar crédito → BUY
    a, b = (larg, cort) if p["tipo"] == "tend" else (cort, larg)
    lado, signo = ("BUY", n) if compra else ("SELL", -n)
    return f"{lado} {signo:+d} VERTICAL SPX 100 (Weeklys) {venc} {a:.0f}/{b:.0f} {'CALL' if sd == 'c' else 'PUT'} @{abs(precio):.2f} LMT"


# ------------------------------------------------------------------ segunda opinión de Claude (capa de veto)
IA_CFG = HERE / "papel_ia.json"     # {"anthropic_api_key": "...", "modelo": "...", "modo": "registrar"|"activo"}  (no va al repo)
IA_SISTEMA = """Eres el jefe de mesa de Katapunta y revisas, en segundos, cada entrada que propone un bot de papel de SPX 0DTE.
El bot sigue reglas fijas (Playbook v1). Tu trabajo: APROBAR o VETAR esta entrada con criterio de trader profesional,
usando el régimen de gamma (precio vs zero γ), los niveles GEX (major +/−, techo/piso, muros), la tendencia multi-temporalidad,
el momentum, el tape (flujo de opciones grandes), el pre-market del día y las lecciones de la bitácora.
Para juzgar la salida usa "valor_estimado": la salida por nivel ocurre a media sesión, así que lo que cuenta es el valor
del spread AHORA con SPX en ese nivel (valor_spread_ahora_en_nivel), no su valor intrínseco a vencimiento.
Veta solo con una razón concreta y verificable en los datos (p. ej. entrar contra el flujo, nivel GEX demasiado cerca, evento
de alto impacto inminente, movimiento ya agotado, rango esperado consumido). Si los datos no dan razón clara, aprueba.
Responde SOLO un JSON: {"decision":"aprobar"|"vetar","confianza":0-100,"razon":"una frase en español, máx 25 palabras"}"""

# Herramienta forzada: la API devuelve el JSON ya estructurado (adiós JSONDecodeError por prosa o respuesta cortada).
ESQ_VETO = {"type": "object", "required": ["decision", "confianza", "razon"], "properties": {
    "decision": {"type": "string", "enum": ["aprobar", "vetar"]},
    "confianza": {"type": "integer", "minimum": 0, "maximum": 100},
    "razon": {"type": "string", "description": "una frase en español, máx 25 palabras"}}}
ESQ_CLASIF = {"type": "object", "required": ["tipo", "direccion", "confianza", "razon"], "properties": {
    "tipo": {"type": "string", "enum": ["tendencia", "rango", "pin"]},
    "direccion": {"type": "string", "enum": ["alcista", "bajista", "neutral"]},
    "confianza": {"type": "integer", "minimum": 0, "maximum": 100},
    "razon": {"type": "string", "description": "una frase en español, máx 25 palabras"}}}


def json_de_texto(txt):
    """Respaldo si la respuesta llega como texto: toma el primer objeto JSON válido (ignora prosa y ```json)."""
    dec = json.JSONDecoder()
    i = txt.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(txt, i)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        i = txt.find("{", i + 1)
    raise ValueError("sin JSON en la respuesta")


def pedir_json(cfg, sistema, datos, nombre, esquema, max_tokens=1024):
    """Una consulta a Claude que regresa un dict. Si falla, el error trae stop_reason y el inicio del texto para diagnosticar."""
    import urllib.request
    body = {"model": cfg.get("modelo", "claude-sonnet-5-5"), "max_tokens": max_tokens, "system": sistema,
            "tools": [{"name": nombre, "description": "Entrega la respuesta estructurada.", "input_schema": esquema}],
            "tool_choice": {"type": "tool", "name": nombre},
            "messages": [{"role": "user", "content": json.dumps(datos, ensure_ascii=False, default=str)}]}
    import urllib.error
    def enviar(b):
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(b).encode(),
                                     headers={"x-api-key": cfg["anthropic_api_key"], "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    try:
        j = enviar(body)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        # 400: el modelo no aceptó la herramienta forzada → misma pregunta en texto (el respaldo tolera prosa y ```json)
        detalle = e.read()[:300].decode("utf-8", "replace")
        print("IA 400 con herramienta:", detalle)
        b2 = {k: v for k, v in body.items() if k not in ("tools", "tool_choice")}
        b2["system"] = sistema + "\nResponde ÚNICAMENTE con un objeto JSON con estos campos: " + ", ".join(esquema["required"]) + "."
        try:
            j = enviar(b2)
        except urllib.error.HTTPError as e2:
            raise ValueError(f"HTTP {e2.code}: {e2.read()[:300].decode('utf-8', 'replace')} (con herramienta: {detalle})")
    for b in j.get("content", []):
        if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
            return b["input"]
    txt = "".join(b.get("text", "") for b in j.get("content", []))
    try:
        return json_de_texto(txt)
    except ValueError:
        raise ValueError(f"stop={j.get('stop_reason')} texto={txt[:200]!r}")


class Veto:
    def __init__(self):
        self.cfg, self.extra, self.dia, self.aviso_llave = {}, {}, None, False
        try:
            self.cfg = json.loads(IA_CFG.read_text(encoding="utf-8")) if IA_CFG.exists() else {}
        except ValueError:
            print("papel_ia.json no es JSON válido")
        k = self.cfg.get("anthropic_api_key", "")
        self.ok = bool(k) and "PEGA_AQUI" not in k
        self.modo = self.cfg.get("modo", "registrar")

    def contexto_dia(self, hoy):
        """Una vez al día: pre-market y lecciones recientes de la bitácora (públicos)."""
        if self.dia == hoy:
            return self.extra
        import urllib.request
        ex = {}
        try:
            with urllib.request.urlopen(f"https://premarket.katapunta.com/premarket.json?t={int(time.time())}", timeout=15) as r:
                pm = json.loads(r.read())
            ex["premarket"] = {k: pm.get(k) for k in ("fecha", "sesgo", "rango_esperado", "niveles", "eventos", "escenarios")}
        except Exception as e:
            ex["premarket"] = f"no disponible ({e.__class__.__name__})"
        try:
            with urllib.request.urlopen(f"https://0dte.katapunta.com/bitacora.json?t={int(time.time())}", timeout=15) as r:
                bj = json.loads(r.read())
            lec = [f"{o['fecha']}: {o['leccion']}" for o in bj.get("ops", []) if o.get("leccion")][-10:]
            ex["lecciones"] = lec or "sin lecciones registradas"
        except Exception as e:
            ex["lecciones"] = f"no disponible ({e.__class__.__name__})"
        self.dia, self.extra = hoy, ex
        return ex

    def consultar(self, bot, p, r, c, ts):
        if not self.ok:
            return None
        import urllib.request, urllib.error
        datos = {
            "hora_et": et(ts), "propuesta": {"estructura": bot.estructura(p), "regla": p["regla"], "precio": p["entrada"],
                                             "contratos": p["n"], "ancho": p["ancho"], "objetivo": p.get("obj"), "ancla_stop": p.get("ancla")},
            "tablero": {k: r.get(k) for k in ("px", "zg", "mp", "mn", "techo", "piso", "cw", "pw", "tf", "tf2", "mom", "em", "rem", "reg", "flips")},
            "ruta_spx_ultimos_30min": [f"{et(t)} {x:.1f}" for t, x in list(bot.ruta)[-30:][::5]],
            "tape": (p.get("ctx") or {}).get("tape") or bot.tape or "sin tape",
            "valor_estimado": valor_en_nivel(bot, p, r, ts),
            "dia_del_bot": {"pnl": bot.pnl_dia(), "cerradas": [f"{o['entra']}-{o['sale']} {o['estructura']} {o['pnl']:+.0f}" for o in bot.ops],
                            "abiertas": [bot.estructura(x) for x in bot.abiertas]},
            **self.contexto_dia(bot.f),
        }
        t0 = time.time()
        try:
            res = pedir_json(self.cfg, IA_SISTEMA, datos, "veredicto", ESQ_VETO)
            res = {"decision": res.get("decision"), "confianza": res.get("confianza"), "razon": res.get("razon"),
                   "seg": round(time.time() - t0, 1), "modo": self.modo}
        except urllib.error.HTTPError as e:
            res = {"error": f"HTTP {e.code}"}
            if e.code in (401, 403) and not self.aviso_llave:
                self.aviso_llave = True
                mandar_yo("PAPEL · Llave de IA inválida o vencida", "La segunda opinión de Claude está apagada. "
                       "Crea una llave nueva en console.anthropic.com y pégala en papel_ia.json.", ["warning"], 4)
        except Exception as e:
            res = {"error": e.__class__.__name__, "detalle": str(e)[:300]}
        try:
            with open(HIST / f"ia_{bot.f}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": ts, "pregunta": datos, "respuesta": res}, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass
        return res


IA_CLASIF = """Eres el jefe de mesa de Katapunta. Con los rasgos del día de SPX hasta ahora (y el pre-market), clasifica cómo
probablemente se comportará el RESTO de la sesión: "tendencia" (sigue en una dirección), "rango" (oscila entre niveles)
o "pin" (se queda pegado a un strike/major positivo hacia el cierre). Responde SOLO un JSON:
{"tipo":"tendencia"|"rango"|"pin","direccion":"alcista"|"bajista"|"neutral","confianza":0-100,"razon":"una frase, máx 25 palabras"}"""


def ia_clasificar(veto, fecha, ts, f):
    if not veto or not veto.ok:
        return None
    datos = {"hora_et": et(ts), "rasgos": f, **veto.contexto_dia(fecha)}
    try:
        return pedir_json(veto.cfg, IA_CLASIF, datos, "clasificacion", ESQ_CLASIF)
    except Exception as e:
        return {"error": e.__class__.__name__, "detalle": str(e)[:300]}


def valor_en_nivel(bot, p, r, ts):
    """Lo que valdría el spread AHORA si SPX estuviera ya en el objetivo (débito) o en el ancla del stop (venta).
    Es valor de salida intradía (no a vencimiento): así se evalúa la salida por nivel."""
    try:
        iv = iv_de(r, ts, bot.pb["cierre"])
        nivel = p.get("obj") if p["tipo"] == "tend" else p.get("ancla")
        if not nivel:
            return None
        v = valor_bs(p["legs"], nivel, ts, iv, bot.pb["cierre"]) + p.get("ajuste", 0)
        venc = intrinseco(p["legs"], nivel)
        return {"nivel_spx": nivel, "valor_spread_ahora_en_nivel": round(v, 2), "valor_spread_a_vencimiento_en_nivel": round(venc, 2),
                "pnl_si_sale_en_nivel": round((v - p["entrada"]) * 100 * p["n"]), "precio_entrada": p["entrada"],
                "nota": "salida por nivel = intradía, antes del vencimiento"}
    except Exception:
        return None


def ia_txt(ia):
    if not ia:
        return ""
    if ia.get("error"):
        return f"IA: sin respuesta ({ia['error']})"
    return f"IA: {'✅ aprueba' if ia.get('decision') == 'aprobar' else '⛔ vetaría'} ({ia.get('confianza')}%) · {ia.get('razon')}"


# ------------------------------------------------------------------ registro del tape (para afinar el Playbook)
class TapeLog:
    """Cada minuto guarda la foto de tape.json (la publica tape_schwab.py) en historial/tape_min_FECHA.jsonl:
    neto acumulado, strikes con más prima y los prints NUEVOS de ese minuto. Al cierre la sube a la rama live."""

    def __init__(self, hoy):
        import collections
        self.hoy, self.f = hoy, HIST / f"tape_min_{hoy}.jsonl"
        self.vistos, self.min_ult, self.ult = set(), None, None
        self.min_prev_ok = False        # en la primera foto los bloques ya existían: no son nuevos
        self.flujo = collections.deque(maxlen=5)       # (dC, dP) de prima neta por minuto, últimos 5 min

    def tick(self):
        m = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).strftime("%H:%M")
        if m == self.min_ult:
            return
        self.min_ult = m
        try:
            import flightdeck_recolector as R
            st, j = R.gh_req(R.gh_token(), "GET", f"/repos/{R.REPO}/contents/tape.json?ref={R.BRANCH}")
            if st != 200:
                return
            snap = json.loads(base64.b64decode(j["content"]))
        except Exception as e:
            print("tape:", e)
            return
        nuevos = []
        for p in snap.get("prints", []):
            k = (p.get("h"), p.get("k"), p.get("t"), p.get("n"))
            if k not in self.vistos:
                self.vistos.add(k)
                nuevos.append(p)
        if self.ult is None:            # primera foto: los prints ya estaban, no son flujo de este minuto
            nuevos_flujo = []
        else:
            nuevos_flujo = nuevos
        dC = sum(p["prem"] * (1 if p["lado"] == "B" else -1 if p["lado"] == "V" else 0) for p in nuevos_flujo if p.get("t") == "C")
        dP = sum(p["prem"] * (1 if p["lado"] == "B" else -1 if p["lado"] == "V" else 0) for p in nuevos_flujo if p.get("t") == "P")
        self.flujo.append((dC, dP))
        self.ult = {"neto": snap.get("neto"), "flujo5": {"C": sum(x[0] for x in self.flujo), "P": sum(x[1] for x in self.flujo)},
                    "spot": snap.get("spot"), "edad": int(time.time()) - int(snap.get("ts") or 0)}
        bl_nuevos = []                  # Tape v2: bloques acumulados que no estaban en la foto anterior
        for b in snap.get("bloques", []):
            k = ("bl", b.get("h_ini"), b.get("h"), b.get("k"), b.get("t"))
            if k not in self.vistos:
                self.vistos.add(k)
                bl_nuevos.append(b)
        linea = {"ts": m, "spot": snap.get("spot"), "neto": snap.get("neto"), "flujo5": self.ult["flujo5"],
                 "edad": self.ult["edad"], "strikes": snap.get("strikes", [])[:6], "nuevos": nuevos,
                 "spreads": len(snap.get("spreads", []))}
        if snap.get("v") == 2:
            linea.update({"bloques_nuevos": bl_nuevos if self.min_prev_ok else [], "liq": snap.get("liq"),
                          "apertura": (snap.get("apertura") or [])[:3], "agr": snap.get("agr")})
            self.ult.update({"liq": snap.get("liq"), "apertura": (snap.get("apertura") or [])[:3],
                             "bloques": [b for b in snap.get("bloques", [])][:3]})
        self.min_prev_ok = True
        with open(self.f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(linea, separators=(",", ":"), ensure_ascii=False) + "\n")

    def subir(self):
        if not self.f.exists():
            return
        try:
            import flightdeck_recolector as R
            tok = R.gh_token()
            path = f"/repos/{R.REPO}/contents/historial/tape_min_{self.hoy}.jsonl.gz"
            st, j = R.gh_req(tok, "GET", path + f"?ref={R.BRANCH}")
            body = {"message": "tape_min " + self.hoy, "branch": R.BRANCH,
                    "content": base64.b64encode(gzip.compress(self.f.read_bytes())).decode()}
            if st == 200 and j.get("sha"):
                body["sha"] = j["sha"]
            st, j = R.gh_req(tok, "PUT", path, body)
            print(f"tape del día → GitHub: {'ok' if st in (200, 201) else st}")
        except Exception as e:
            print("subir tape:", e)


# ------------------------------------------------------------------ clasificador del tipo de día (fase 1: solo observa)
CLASIF_HORAS = (9 * 60, 10 * 60, 12 * 60)     # CDMX → 10:00, 11:00 y 13:00 ET


def rasgos(dia, r):
    """Rasgos del día hasta ahora. dia = [(ts, lectura)] desde las 8:30 CDMX."""
    px0 = dia[0][1]["px"]
    em = r.get("em") or [px0 - 50, px0 + 50]
    mitad = max(1.0, (em[1] - em[0]) / 2)
    xs = [x["px"] for _, x in dia]
    zg = [zg_valido(x) for _, x in dia]
    g = [0 if z is None else 1 if x > z + PB["gtol"] else -1 if x < z - PB["gtol"] else 0 for x, z in zip(xs, zg)]
    cruces = sum(1 for a, b in zip(g, g[1:]) if a and b and a != b)
    tf2, mom = r.get("tf2") or [0, 0, 0], (r.get("mom") or [0, 0, 0])
    chg0 = dia[0][1].get("chg") or "0"
    try:
        gap = float(str(chg0).replace("−", "-").replace("%", "").replace("+", ""))
    except ValueError:
        gap = 0.0
    mp, mn = r.get("mp"), r.get("mn")
    return {"px": r["px"], "apertura": px0, "mov": round(r["px"] - px0, 1), "mov_em": round((r["px"] - px0) / mitad, 2),
            "rango_em": round((max(xs) - min(xs)) / mitad, 2), "gap_pct": gap, "gamma": g[-1],
            "gamma_estable": round(sum(1 for x in g[-30:] if x == g[-1]) / max(1, len(g[-30:])), 2), "cruces_zg": cruces,
            "tf2": tf2, "mom": mom, "vs_vwap": round(r["px"] - (r.get("vw") or r["px"]), 1),
            "dist_mp": round(r["px"] - mp, 1) if mp else None, "dist_mn": round(r["px"] - mn, 1) if mn else None,
            "mp": mp, "mn": mn, "zg": zg_valido(r), "con_gamma": zg_valido(r) is not None,
            "vix": (r.get("vix") or {}).get("chg"), "macro": r.get("macro")}


def clasificar_reglas(f):
    pts = {"tendencia": 0, "rango": 0, "pin": 0}
    rz = []
    al = f["tf2"][0] if f["tf2"][0] and f["tf2"][0] == f["tf2"][1] == f["tf2"][2] else 0
    if abs(f["mov_em"]) >= 0.5:
        pts["tendencia"] += 2; rz.append(f"ya recorrió {abs(f['mov_em']):.0%} del rango esperado")
    elif abs(f["mov_em"]) < 0.3:
        pts["rango"] += 2; pts["pin"] += 1; rz.append(f"solo {abs(f['mov_em']):.0%} del rango esperado")
    if al:
        pts["tendencia"] += 2; rz.append(f"tendencia {'alcista' if al > 0 else 'bajista'} alineada en 5/15/60 min")
        if (f["mov"] > 0) == (al > 0) and f["mom"][0] == al:
            pts["tendencia"] += 1
    else:
        pts["rango"] += 1; rz.append("sin tendencia alineada en 5/15/60 min")
    if f["gamma"] < 0 and f["gamma_estable"] >= 0.8:
        pts["tendencia"] += 2; rz.append("gamma negativa estable")
    elif f["gamma"] > 0 and f["gamma_estable"] >= 0.8:
        pts["rango"] += 1; pts["pin"] += 1; rz.append("gamma positiva estable")
    if f["cruces_zg"] >= 2:
        pts["rango"] += 1; rz.append(f"{f['cruces_zg']} cruces del zero γ")
    if abs(f["gap_pct"]) >= 0.6:
        pts["tendencia"] += 1; rz.append(f"gap de {f['gap_pct']:+.1f}%")
    if f["dist_mp"] is not None and abs(f["dist_mp"]) <= 10 and f["gamma"] > 0:
        pts["pin"] += 2; rz.append(f"a {abs(f['dist_mp']):.0f} pts del major +")
    tot = sum(pts.values()) or 1
    tipo = max(pts, key=pts.get)
    conf = round(100 * pts[tipo] / tot)
    if conf < 45:
        tipo = "incierto"
    elif tot < 3:
        tipo = "incierto"; rz.append(f"evidencia mínima ({tot} pts)")
    elif not f.get("con_gamma", True):
        tipo = "incierto"; rz.append("sin lectura γ")
    return {"tipo": tipo, "conf": conf, "puntos": pts, "razones": rz}


def etiquetar_dia(dia, mp_final=None):
    """La "verdad" al cierre: tendencia / rango / pin."""
    if len(dia) < 60:
        return None
    xs = [x["px"] for _, x in dia]
    em = dia[0][1].get("em") or [xs[0] - 50, xs[0] + 50]
    mitad = max(1.0, (em[1] - em[0]) / 2)
    mov, rng = xs[-1] - xs[0], max(xs) - min(xs)
    mp = mp_final or dia[-1][1].get("mp")
    ult = [x["px"] for t, x in dia if mins(t) >= PB["cierre"] - 60]
    if mp and ult and max(abs(x - mp) for x in ult) <= 10:
        return {"tipo": "pin", "detalle": f"última hora a ≤10 pts del major + {mp}"}
    if abs(mov) >= 0.6 * rng and abs(mov) >= 0.5 * mitad:
        return {"tipo": "tendencia", "detalle": f"{'alcista' if mov > 0 else 'bajista'}: {mov:+.0f} pts de {rng:.0f} de rango ({abs(mov) / mitad:.0%} del esperado)"}
    return {"tipo": "rango", "detalle": f"movió {mov:+.0f} pts en {rng:.0f} de rango"}


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
        self.avisar = False             # solo en vivo
        self.tape = None                # último resumen del tape (solo en vivo)
        self.veto = None                # segunda opinión de Claude (solo en vivo)
        self.vetadas = []
        import collections
        self.ruta = collections.deque(maxlen=60)
        self.dia = []                   # [(ts, lectura)] desde las 8:30 CDMX, para el clasificador
        self.clasif = []
        self.real = None
        self.previo = {}                # clave → {ia, ctx} de operaciones ya publicadas hoy (para reanudar)
        self.tape_hist = {}             # "HH:MM" → resumen del tape guardado ese minuto
        self.ult_min = None             # último minuto procesado (ignora lecturas repetidas)
        self.peor = 0.0                 # Fase 0: mínimo del día de (P&L cerrado − pérdida máxima de lo abierto)

    # -- contexto ------------------------------------------------------
    def ctx(self, r):
        px, zgv = r["px"], zg_valido(r)
        zg = zgv if zgv is not None else px
        g = 0 if zgv is None else 1 if px > zg + self.pb["gtol"] else -1 if px < zg - self.pb["gtol"] else 0
        tf2 = r.get("tf2") or [0, 0, 0]
        T = tf2[0] if tf2[0] == tf2[1] else 0
        mom = (r["mom"] or [0])[0] if "mom" in r else (tf2[0] if tf2[0] == tf2[1] else 0)   # días sin momentum: usa la tendencia
        return {"px": px, "zg": round(zg, 1), "g": g, "T": T, "mom": mom, "mp": r.get("mp"), "mn": r.get("mn"),
                "techo": (r.get("techo") or [None])[0], "piso": (r.get("piso") or [None])[0], "tf2": tf2,
                "cw": (r.get("cw") or [None])[0], "pw": (r.get("pw") or [None])[0],
                "con_gamma": zgv is not None, "edad_g": None, "g_prop": self.g_propia(r), "zge": r.get("zge"),
                "fuente_g": "manual" if zgv is not None else None}

    @staticmethod
    def g_propia(r):
        """Régimen según el GEX propio del recolector (cadena de Schwab): +1 / −1, o 0 si es incierto."""
        reg = r.get("reg") or {}
        return 0 if reg.get("inc") or reg.get("d0") not in (1, -1) else reg["d0"]

    def usar_propia(self, c):
        """Candidata gamma_propia: cambia el régimen al del GEX propio. Devuelve True si lo aplicó."""
        if not self.pb.get("gamma_propia") or not c["g_prop"]:
            return False
        c["g"], c["con_gamma"], c["fuente_g"] = c["g_prop"], True, "propia"
        if not c["con_gamma"] or c["zg"] == c["px"]:
            c["zg"] = c["zge"] or c["px"]
        return True

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

    def bp_uso(self):
        """Buying power en uso = pérdida máxima de lo abierto (en verticales Schwab aparta exactamente eso)."""
        return sum((p["ancho"] + p["entrada"] if p["entrada"] < 0 else p["entrada"]) * 100 * p["n"] for p in self.abiertas)

    def ctr(self, riesgo_spread):
        return max(1, min(self.pb["max_ctr"], int(self.pb["riesgo"] // max(1.0, riesgo_spread * 100))))

    # -- entradas ------------------------------------------------------
    def abrir(self, ts, r, c, tipo, dire, legs, precio, ancho, regla, ancla=None, objetivo=None, natural=None):
        credito = precio < 0
        riesgo = ancho + precio if credito else precio          # precio <0 = crédito
        n = self.ctr(riesgo)
        libre = self.pb["capital"] - self.bp_uso()
        n = min(n, int(libre // max(1.0, riesgo * 100)))
        if n < 1:
            return                                  # no alcanza el capital del bot
        p = {"id": f"{self.f}-{len(self.ops) + len(self.abiertas) + 1}", "tipo": tipo, "dir": dire, "legs": legs,
             "entrada": round(precio, 2), "n": n, "ancho": ancho, "regla": regla, "ancla": ancla, "obj": objetivo,
             "t_in": ts[:5], "estimado": bool(r.get("_bs")), "ctx": {**{k: c[k] for k in ("px", "zg", "g", "T", "mp", "mn", "tf2", "edad_g")},
                     "tape": self.tape if self.avisar else self.tape_hist.get(ts[:5].zfill(5), self.tape)}, "max": 0.0, "min": 0.0}
        iv = iv_de(r, ts, self.pb["cierre"])
        p["ajuste"] = precio - valor_bs(legs, r["px"], ts, iv, self.pb["cierre"])   # skew/spread real vs BS al entrar
        p["m_in"] = mins(ts)
        p["natural"] = None if p["estimado"] else natural   # Fase 0: precio natural (bid/ask) al entrar, con signo como "entrada"
        prev = self.previo.get(self.clave(p))          # al reanudar: conserva la opinión de Claude y el tape originales
        if prev and not self.avisar:
            if prev.get("ia"):
                p["ia"] = prev["ia"]
            if (prev.get("ctx") or {}).get("tape"):
                p["ctx"]["tape"] = prev["ctx"]["tape"]
        if self.veto and self.avisar:
            p["ia"] = self.veto.consultar(self, p, r, c, ts)
            if self.veto.modo == "activo" and (p["ia"] or {}).get("decision") == "vetar":
                self.vetadas.append({"t": et(ts), "estructura": self.estructura(p), "regla": regla, "ia": p["ia"]})
                mandar(f"PAPEL · Entrada vetada por IA: {self.estructura(p)}", f"{et(ts)} ET\n{ia_txt(p['ia'])}\nPor qué la proponía: {regla}",
                       ["no_entry"], 3)
                return
        self.abiertas.append(p)
        self.n_ent += 1
        if self.avisar:
            pb = self.pb
            if tipo == "tend":
                plan = (f"TP: spread en {precio + pb['deb_tp'] * (ancho - precio):.2f}" + (f" o SPX en {objetivo:.0f}" if objetivo else "")
                        + f" · SL: spread en {precio * (1 - pb['deb_sl']):.2f} o tendencia volteada")
            elif tipo == "venta":
                plan = f"TP: recomprar en {-precio * (1 - pb['ven_tp']):.2f} · Stop: SPX rompe {ancla:.0f} por {pb['nivel_buf']:.0f} pts 2 min"
            else:
                plan = "Al vencimiento · Stop: SPX sale de los cortos por 3 pts 2 min"
            mandar(f"PAPEL · Abrir {self.estructura(p)}",
                   f"{et(ts)} ET · SPX {c['px']:,.2f}\n{patas(p, True)}\n{orden_tos(p, self.f, True, precio)}\n"
                   f"{'Débito' if precio > 0 else 'Crédito'} {abs(precio):.2f} · riesgo ${(ancho + precio if precio < 0 else precio) * 100 * n:,.0f}\n"
                   f"Por qué: {regla}\n{plan}" + (f"\n{ia_txt(p.get('ia'))}" if p.get("ia") else ""), ["green_circle"], 5)

    @staticmethod
    def clave(p):
        return p["t_in"][:5] + "|" + ",".join(f"{sd}{K:.0f}{q:+d}" for sd, K, q in sorted(p["legs"]))

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
        if not c["con_gamma"] and not self.usar_propia(c):
            return                                  # sin lectura γ no hay régimen: no se abre nada
        c["edad_g"] = edad_gamma(ts, r) if c["fuente_g"] == "manual" else None
        gamma_vieja = c["edad_g"] is not None and c["edad_g"] > pb["gamma_edad"]
        if gamma_vieja and self.usar_propia(c):
            gamma_vieja = False                     # lectura manual vieja → régimen del GEX propio
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
                if pb["a_recorrido"] and obj and abs(c["px"] - obj) < pb["a_recorrido"]:
                    return                          # ya no queda recorrido hasta el objetivo
                if pb["obj_pasa_largo"] and obj and not ((c["T"] < 0 and obj <= x[2]) or (c["T"] > 0 and obj >= x[2])):
                    obj = None                      # el objetivo no pasa el strike largo: sale por TP de %
                self.abrir(ts, r, c, "tend", c["T"], [(sd, x[2], 1), (sd, x[3], -1)], deb, pb["deb_ancho"],
                           f"A · gamma negativa ({c['px']:.0f} < zero γ {c['zg']:.0f}) y tendencia "
                           f"{'alcista' if c['T'] > 0 else 'bajista'} en 5 y 15 min con momentum → débito a favor; objetivo {obj}",
                           objetivo=obj, natural=x[4])
                return
        # B · gamma positiva → venta anclada a un nivel (soporte abajo / resistencia arriba)
        tend_dia = pb["b_sin_tendencia"] and self.clasif and self.clasif[-1]["reglas"]["tipo"] == "tendencia"
        if c["g"] == 1 and m <= pb["ultima_venta"] and not gamma_vieja and not tend_dia:
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
                               f"(corto {x[2]:.0f}, δ {x[1]:.2f}); stop solo si se rompe {ancla:.0f}", ancla=ancla, natural=-x[4])
                    return
        # C · IC de pin al final del día
        a, b = pb["pin"]
        pin_t = (r.get("pin") or [None])[0]
        ult = [x for _, x in list(self.ruta)[-30:]]
        quieto = pb["c_quietud"] and pin_t and len(ult) >= 25 and max(ult) - min(ult) <= pb["c_quietud"] \
            and abs(c["px"] - pin_t) <= pb["pin_dist"]
        clasico = c["g"] == 1 and c["mp"] and not gamma_vieja and abs(c["px"] - c["mp"]) <= pb["pin_dist"]
        if a <= m <= b and (clasico or quieto) \
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
                           (f"C · pin por quietud: SPX a {abs(c['px'] - pin_t):.0f} pts del PIN {pin_t:.0f} y rango de 30 min de "
                            f"{max(ult) - min(ult):.0f} pts → IC δ0.20 al vencimiento; stop solo si sale del corto") if (quieto and not clasico) else
                           f"C · pin: gamma positiva y precio a {abs(c['px'] - c['mp']):.0f} pts del major + {c['mp']:.0f} → "
                           f"IC δ0.20 al vencimiento; stop solo si sale del corto", natural=-(xc[4] + xp[4]))

    # -- gestión -------------------------------------------------------
    def valor(self, p, ts, r, cad):
        """Valor del spread (positivo = vale a favor de un largo). Real (cadena) o Black-Scholes con ajuste."""
        if cad:
            try:
                v = sum(q * cad[(sd, K)] for sd, K, q in p["legs"])
                p["marca"] = "cadena"
                return v
            except KeyError:
                pass
        p["marca"] = "modelo"                      # Fase 0: queda registrado que este valor es Black-Scholes, no cotización
        iv = iv_de(r, ts, self.pb["cierre"])
        frac = max(0.0, (self.pb["cierre"] - mins(ts)) / max(1, self.pb["cierre"] - p["m_in"]))
        return valor_bs(p["legs"], r["px"], ts, iv, self.pb["cierre"]) + p["ajuste"] * frac

    def medir(self, p, pnl, motivo):
        """Fase 0: P&L con comisiones y con entrada al precio natural. Solo informa; nada lo usa para decidir."""
        vence = motivo.startswith("Venc")
        com = round(len(p["legs"]) * p["n"] * COM_PATA * (1 if vence else 2), 2)
        nat = p.get("natural")
        pnl_nat = round(pnl + (p["entrada"] - nat) * 100 * p["n"], 0) if nat is not None else None
        # Torneo (23-nov): natural también a la salida. Sin bid/ask guardado, se usa el medio spread h visto al entrar.
        pnl_natural = None
        if nat is not None:
            h = abs(nat - (p["entrada"] - self.pb["slip"]))
            sale_nat = p["_salida"] if vence else (p["_salida"] + self.pb["slip"]) - h
            pnl_natural = round((sale_nat - nat) * 100 * p["n"], 2)
        return {"com": com, "pnl_neto": round(pnl - com, 2), "pnl_entrada_nat": pnl_nat,
                "pnl_natural": pnl_natural, "pnl_neto_nat": round(pnl_natural - com, 2) if pnl_natural is not None else None,
                "marca_salida": "liquidación" if vence else p.get("marca", "modelo")}

    def cerrar(self, p, ts, precio, motivo):
        pnl = round((precio - p["entrada"]) * 100 * p["n"], 0)
        p["_salida"] = precio
        self.abiertas.remove(p)
        m = mins(ts)
        self.ult_cierre[p["dir"]] = m
        if pnl < 0:
            self.ult_perdida = m
        self.ops.append({"id": p["id"], "fecha": self.f, "estructura": self.estructura(p), "tipo": p["tipo"],
                         "entra": et(p["t_in"]), "sale": et(ts), "entrada": p["entrada"], "salida": round(precio, 2),
                         "n": p["n"], "pnl": pnl, "regla": p["regla"], "estimado": p.get("estimado", False), "motivo": motivo, "ctx": p["ctx"], "ia": p.get("ia"), "clave": self.clave(p),
                         "mejor": round((p["max"]) * 100 * p["n"], 0), "peor": round(p["min"] * 100 * p["n"], 0),
                         **self.medir(p, pnl, motivo)})
        if self.avisar:
            mandar(f"PAPEL · Cerrar {self.estructura(p)} · {'+' if pnl >= 0 else '−'}${abs(pnl):,.0f}",
                   f"{et(ts)} ET\n" + (patas(p, False) + "\n" + orden_tos(p, self.f, False, precio) if not motivo.startswith("Venc") else "Expira: no hay que mandar orden")
                   + f"\nMotivo: {motivo}\nDía en papel: {'−' if self.pnl_dia() < 0 else '+'}${abs(self.pnl_dia()):,.0f}",
                   ["white_check_mark" if pnl >= 0 else "red_circle"], 5)

    def gestionar(self, ts, r, c, cad):
        pb, m = self.pb, mins(ts)
        for p in list(self.abiertas):
            v = self.valor(p, ts, r, cad)
            g = v - p["entrada"]
            p["max"], p["min"] = max(p["max"], g), min(p["min"], g)
            if p["tipo"] == "tend":
                deb = p["entrada"]
                if v >= deb + pb["deb_tp"] * (p["ancho"] - deb) - EPS:
                    self.cerrar(p, ts, v - pb["slip"], f"TP: capturó {pb['deb_tp']:.0%} de la ganancia máxima"); continue
                if p["obj"] and ((p["dir"] > 0 and c["px"] >= p["obj"]) or (p["dir"] < 0 and c["px"] <= p["obj"])):
                    self.cerrar(p, ts, v - pb["slip"], f"TP: el precio llegó al nivel objetivo {p['obj']}"); continue
                if v <= deb * (1 - pb["deb_sl"]) + EPS:
                    self.cerrar(p, ts, v - pb["slip"], f"SL: el spread perdió {pb['deb_sl']:.0%} del débito"); continue
                if c["tf2"][0] == -p["dir"] and c["tf2"][1] == -p["dir"]:
                    self.cerrar(p, ts, v - pb["slip"], "SL: la tendencia de 5 y 15 min se volteó"); continue
            else:
                cred = -p["entrada"]
                if p["tipo"] == "venta" and -v <= cred * (1 - pb["ven_tp"]) + EPS:
                    self.cerrar(p, ts, v - pb["slip"], f"TP: recompra al {1 - pb['ven_tp']:.0%} del crédito"); continue
                if -v >= cred * pb["ven_cat"] - EPS:
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

    def clasificar(self, ts, r, m):
        f = rasgos(self.dia, r)
        cr = clasificar_reglas(f)
        ci = ia_clasificar(self.veto, self.f, ts, f) if (self.veto and self.avisar) else None
        self.clasif.append({"m": m, "hora": et(f"{m // 60}:{m % 60:02d}"), "reglas": cr, "ia": ci, "rasgos": f})
        if self.avisar:
            linea_ia = (f"\nClaude: {ci.get('tipo')} {ci.get('direccion', '')} ({ci.get('confianza')}%) · {ci.get('razon')}"
                        if ci and not ci.get("error") else "")
            mandar(f"PAPEL · Tipo de día ({et(ts)} ET): {cr['tipo']} {cr['conf']}%",
                   f"Reglas: {cr['tipo']} ({cr['conf']}%) · " + "; ".join(cr["razones"]) + linea_ia
                   + "\nFase 1: solo observa, no cambia las reglas del bot.", ["bar_chart"], 4)

    def liquidar(self, ts, px):
        for p in list(self.abiertas):
            self.cerrar(p, ts, intrinseco(p["legs"], px), f"Vencimiento: SPX liquidó en {px:.2f}")

    def paso(self, ts, r, cad=None):
        if self.ult_min is not None and ts[:5].zfill(5) <= self.ult_min:
            return                                  # lectura repetida del mismo minuto (dos recolectores)
        self.ult_min = ts[:5].zfill(5)
        self.ruta.append((ts, r["px"]))
        if mins(ts) >= 8 * 60 + 30:
            self.dia.append((ts, r))
            h = [x for x in CLASIF_HORAS if mins(ts) >= x and not any(c["m"] == x for c in self.clasif)]
            if h and len(self.dia) >= 20:
                self.clasificar(ts, r, h[-1])
        c = self.ctx(r)
        if mins(ts) >= self.pb["cierre"] - 1:
            self.liquidar(ts, c["px"])
            return
        self.gestionar(ts, r, c, cad)
        self.buscar(ts, r, c)
        self.peor = min(self.peor, self.pnl_dia() - self.bp_uso())

    def resumen(self):
        n = len(self.ops)
        g = sum(1 for o in self.ops if o["pnl"] > 0)
        return {"fecha": self.f, "pnl": self.pnl_dia(), "ops": n, "ganadoras": g, "nota": self.nota, "vetadas": self.vetadas,
                "capital": self.pb["capital"], "bp_uso": round(self.bp_uso()),
                "fase0": {"peor_posible": round(self.peor), "pnl_neto": round(sum(o["pnl_neto"] for o in self.ops), 2),
                          "comisiones": round(sum(o["com"] for o in self.ops), 2),
                          "salidas_modelo": sum(1 for o in self.ops if o["marca_salida"] == "modelo"),
                          "pnl_natural": round(sum(o.get("pnl_natural") or 0 for o in self.ops), 2),
                          "pnl_neto_nat": round(sum(o.get("pnl_neto_nat") or 0 for o in self.ops), 2),
                          "sin_natural": sum(1 for o in self.ops if o.get("pnl_natural") is None)},
                "clasif": [{k: v for k, v in c.items() if k != "m"} for c in self.clasif],
                "real": self.real or (etiquetar_dia(self.dia) if self.dia and (not self.avisar or mins(self.dia[-1][0]) >= self.pb["cierre"] - 5) else None),
                "abiertas": [{"estructura": self.estructura(p), "entra": et(p["t_in"]), "entrada": p["entrada"], "regla": p["regla"],
                              "clave": self.clave(p), "ia": p.get("ia"), "ctx": p["ctx"]} for p in self.abiertas]}


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


def leer_tape_min(fecha):
    """Resumen del tape por minuto ("HH:MM" → {neto, flujo5, spot, edad}) que guardó el bot en vivo."""
    out = {}
    for f in (HIST / f"tape_min_{fecha}.jsonl", HIST / f"tape_min_{fecha}.jsonl.gz"):
        if f.exists():
            txt = gzip.decompress(f.read_bytes()).decode() if f.suffix == ".gz" else f.read_text(encoding="utf-8")
            for ln in txt.splitlines():
                try:
                    j = json.loads(ln)
                    out[j["ts"][:5].zfill(5)] = {"neto": j.get("neto"), "flujo5": j.get("flujo5"), "spot": j.get("spot"), "edad": j.get("edad")}
                except (ValueError, KeyError):
                    pass
            break
    return out


def correr_dia(fecha, pb=None):
    b = Bot(fecha, pb or PB)
    b.tape_hist = leer_tape_min(fecha)
    filas = leer_dia(fecha)
    for ts, r in filas:
        if mins(ts) < 8 * 60 + 30:
            continue
        b.paso(ts, r)
        if mins(ts) >= PB["cierre"] - 1:
            break
    liq = json.loads((HERE / "liquidaciones.json").read_text(encoding="utf-8")) if (HERE / "liquidaciones.json").exists() else {}
    if filas and mins(filas[-1][0]) < PB["cierre"] - 5:
        b.nota = f"El historial termina a las {et(filas[-1][0])} ET (el recolector se detuvo): sin lecturas después de esa hora."
    if b.abiertas and filas:                 # el historial terminó antes de las 14:59
        ts, r = filas[-1]
        b.liquidar("14:59", liq.get(fecha, r["px"]))
    return b


# ------------------------------------------------------------------ publicación
def armar(dias_bots, modo, carril=None):
    """papel.json: "dias" es el CONTROL (Playbook v1). "carril_claude" es el carril propio de Claude (torneo)."""
    try:
        prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    except ValueError:
        prev = {}
    dias = prev.get("dias", {})
    for b in dias_bots:
        dias[b.f] = {**b.resumen(), "modo": modo, "operaciones": b.ops}
    cc = prev.get("carril_claude") or {}
    cdias = cc.get("dias", {})
    for b in carril or []:
        cdias[b.f] = {**b.resumen(), "modo": modo, "operaciones": b.ops,
                      "abiertas": [{"estructura": b.estructura(p), "entra": et(p["t_in"]), "entrada": p["entrada"], "regla": p["regla"]}
                                   for p in b.abiertas]}
    out = {"actualizado": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "modo": modo,
           "playbook": {k: v for k, v in PB.items()}, "dias": dict(sorted(dias.items()))}
    if cdias:
        out["carril_claude"] = {"version": PB_CLAUDE["version"], "playbook": dict(PB_CLAUDE),
                                "cambios": cc.get("cambios") or CAMBIOS_CLAUDE, "dias": dict(sorted(cdias.items()))}
    return out


# Bitácora de cambios del carril Claude (se publica en la página del torneo)
CAMBIOS_CLAUDE = [
    {"fecha": "2026-11-24", "version": "Claude C1",
     "cambios": ["A: TP al 65% de la ganancia máxima (v1: 50%)",
                 "A: no compra si al objetivo le quedan menos de 15 pts",
                 "B: crédito cobrado ≥ 1.50 ya con el deslizamiento (medio ≥ 1.55)"],
     "hipotesis": "A deja correr las ganadoras y evita entradas sin recorrido; B deja de vender por debajo de su propio mínimo.",
     "evidencia": "Repetición de 13 días (3 al 23-nov): +$11,237 contra +$7,839 del v1; 8 días mejores y 1 peor. Con deslizamiento de 0.15: +$9,305 contra +$5,779, 0 peores. Dentro de muestra: solo cuenta lo que haga de aquí en adelante."}
]


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


def tema_alertas():
    """Primera vez: crea un tema secreto de ntfy y lo guarda en papel_alertas.json (no va al repositorio)."""
    if not ALERTAS.exists():
        import secrets
        ALERTAS.write_text(json.dumps({"ntfy": "katapunta-papel-" + secrets.token_hex(5),
                                       "whatsapp": {"telefono": "", "apikey": ""}}, indent=1), encoding="utf-8")
    t = json.loads(ALERTAS.read_text(encoding="utf-8")).get("ntfy")
    print(f"Alertas al celular: en la app ntfy suscríbete al tema  {t}")
    wa = json.loads(ALERTAS.read_text(encoding="utf-8")).get("whatsapp") or {}
    print("WhatsApp: " + ("activo" if wa.get("telefono") and wa.get("apikey") else
          "apagado (pon tu teléfono y la llave de CallMeBot en papel_alertas.json)") + "\n")
    return t


_CANDADO = None


def una_sola_instancia(puerto=47321):
    """Candado: si ya hay un bot corriendo en esta PC, no arranca otro (evita alertas y publicaciones dobles)."""
    global _CANDADO
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", puerto))
        s.listen(1)
    except OSError:
        return False
    _CANDADO = s
    return True


def vivo():
    if not una_sola_instancia():
        print("⚠ Ya hay un bot de papel corriendo en esta computadora. No abro otro. (Cierra esta ventana.)")
        return
    traer_remoto()
    t = tema_alertas()
    hoy = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).date().isoformat()
    try:
        est = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except ValueError:
        est = {}
    b = Bot(hoy)
    b.avisar = True
    b.tape_hist = leer_tape_min(hoy)
    try:                                   # lo ya publicado hoy, para reanudar sin perder la opinión de Claude ni el tape
        prev = (json.loads(OUT.read_text(encoding="utf-8")).get("dias") or {}).get(hoy) or {}
        for o in (prev.get("operaciones") or []) + (prev.get("abiertas") or []):
            if o.get("clave"):
                b.previo[o["clave"]] = {"ia": o.get("ia"), "ctx": o.get("ctx")}
    except Exception:
        pass
    b2 = Bot(hoy, PB_CLAUDE)               # torneo: carril propio de Claude, en silencio (sin alertas ni consulta a la IA)
    b2.avisar = False
    b2.tape_hist = b.tape_hist
    mk = Marcador()
    tl = TapeLog(hoy)
    b.veto = Veto()
    print("Segunda opinión de Claude: " + (f"activa · modo {b.veto.modo}" if b.veto.ok else "apagada (falta la llave en papel_ia.json)") + "\n")
    hecho = 0
    if est.get("fecha") == hoy:          # reinicio a media sesión: re-procesa el historial de hoy sin marcas reales
        print("Reanudando: vuelvo a pasar el historial de hoy.")
    print(f"Bot de papel en marcha ({PB['version']}). Solo papel: no existe código de órdenes. Ctrl+C para salir.\n")
    mandar_yo("PAPEL · Bot en marcha", f"{PB['version']} · empieza a buscar entradas a las {et('09:00')} ET. "
           "Las órdenes vienen escritas como en thinkorswim para el espejo en paperMoney.", ["robot"], 2)
    sin_datos = 0
    primera = True
    while True:
      try:
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
          ahora_m = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6)))
          if 8 * 60 + 30 <= ahora_m.hour * 60 + ahora_m.minute <= PB["cierre"]:
              tl.tick()
              b.tape = tl.ult
              b2.tape = tl.ult
          for i, (ts, r) in enumerate(filas):
              if mins(ts) < 8 * 60 + 30:
                  continue
              cad = None
              if (b.abiertas or b2.abiertas) and i == len(filas) - 1:
                  try:
                      cad = mk.cadena(hoy)
                  except Exception as e:
                      print("  cadena Schwab:", e, "→ marco con Black-Scholes")
              ahora = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6)))
              b.avisar = (ahora.hour * 60 + ahora.minute) - mins(ts) <= 3     # al reanudar, no avisa lo ya pasado
              antes = len(b.ops) + len(b.abiertas)
              b.paso(ts, r, cad)
              try:
                  b2.paso(ts, r, cad)          # un error del carril Claude nunca detiene al control
              except Exception:
                  traceback.print_exc()
                  print("⚠ Error en el carril Claude; el control sigue.")
              if len(b.ops) + len(b.abiertas) != antes or i == len(filas) - 1:
                  print(f"{et(ts)} ET  SPX {r['px']}  abiertas {len(b.abiertas)}  cerradas {len(b.ops)}  P&L ${b.pnl_dia():,.0f}")
          if filas and primera:
              primera = False
              if b.ops or b.abiertas:
                  mandar_yo("PAPEL · Reanudado", "Al ponerse al día con el historial de hoy:\n"
                         + "\n".join(f"Cerrada {o['entra']}→{o['sale']} ET {o['estructura']} {o['pnl']:+,.0f}" for o in b.ops)
                         + ("\n" if b.ops and b.abiertas else "")
                         + "\n".join(f"Abierta desde {et(p['t_in'])} ET: {orden_tos(p, hoy, True, p['entrada'])}" for p in b.abiertas),
                         ["arrows_counterclockwise"], 3)
          if filas:
              STATE.write_text(json.dumps({"fecha": hoy}), encoding="utf-8")
              publicar(armar([b], "vivo", [b2]))
          t = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6)))
          if t.hour * 60 + t.minute >= PB["cierre"] + 2:
              if b.abiertas and filas:
                  b.liquidar(*[(filas[-1][0], filas[-1][1]["px"])][0])
              if b2.abiertas and filas:
                  b2.liquidar(*[(filas[-1][0], filas[-1][1]["px"])][0])
              publicar(armar([b], "vivo", [b2]))
              print(f"Carril Claude: P&L ${b2.pnl_dia():,.0f} ({len(b2.ops)} ops)")
              imprimir(b)
              mandar(f"PAPEL · Cierre del día {'+' if b.pnl_dia() >= 0 else '−'}${abs(b.pnl_dia()):,.0f}",
                     f"{len(b.ops)} operaciones · {sum(1 for o in b.ops if o['pnl'] > 0)} ganadoras", ["checkered_flag"], 3)
              tl.subir()
              print("\nFin del día.")
              break
      except Exception:
        traceback.print_exc()
        print("⚠ Error en la vuelta; el bot sigue corriendo.")
      time.sleep(20)


def probar_ia():
    """Prueba la llave con la última lectura del historial: una consulta real y su respuesta."""
    v = Veto()
    if not v.ok:
        print("Falta la llave en papel_ia.json"); return
    fs = sorted(glob.glob(str(HIST / "20??-??-??.jsonl")))
    if not fs:
        print("No hay historial para probar"); return
    f = Path(fs[-1]).name[:10]
    filas = leer_dia(f)
    b = Bot(f); b.avisar = True
    for ts, r in filas[-30:]:
        b.ruta.append((ts, r["px"]))
    ts, r = filas[-1]
    c = b.ctx(r)
    p = {"tipo": "tend", "dir": -1, "legs": [("p", round(c["px"] / 5) * 5, 1), ("p", round(c["px"] / 5) * 5 - 10, -1)],
         "entrada": 2.5, "n": 2, "ancho": 10, "regla": "PRUEBA · entrada inventada para probar la conexión", "obj": None, "ancla": None}
    print(f"Consultando a Claude con la lectura de {f} {et(ts)} ET…")
    res = v.consultar(b, p, r, c, ts)
    print(res)
    print(ia_txt(res))


def main():
    if "--probar-ia" in sys.argv:
        probar_ia(); return
    if "--replay" in sys.argv:
        q = sys.argv[sys.argv.index("--replay") + 1]
        if q == "todo":
            fechas = sorted({Path(x).name[:10] for x in glob.glob(str(HIST / "*.jsonl*"))})
        else:
            fechas = [q]
        if "--local" not in sys.argv:
            traer_remoto()
        bots, bots2 = [], []
        for f in fechas:
            try:
                b = correr_dia(f)
                b2 = correr_dia(f, PB_CLAUDE)
            except Exception as e:
                print(f, "sin historial:", e)
                continue
            imprimir(b)
            print(f"  carril Claude: ${b2.pnl_dia():,.0f} ({len(b2.ops)} ops)")
            bots.append(b); bots2.append(b2)
        tot = sum(b.pnl_dia() for b in bots)
        n = sum(len(b.ops) for b in bots)
        g = sum(1 for b in bots for o in b.ops if o["pnl"] > 0)
        print(f"\nTOTAL {len(bots)} días · {n} operaciones · {g} ganadoras · P&L ${tot:,.0f}")
        publicar(armar(bots, "replay", bots2))
        return
    vivo()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido.")
    except Exception:
        traceback.print_exc()
