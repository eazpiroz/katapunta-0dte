"""Tape aproximado de SPX 0DTE con el streamer de Schwab (pendiente #14).

Schwab no da time & sales de opciones. Este script se suscribe a LEVELONE_OPTIONS de ~60
contratos SPXW 0DTE cerca del precio y reconstruye los prints a partir de los saltos de
TOTAL_VOLUME, clasificándolos contra el bid/ask del momento:
    precio >= ask  → B (compra agresiva)      precio <= bid → V (venta agresiva)      otro → M (medio)
Filtro: solo saltos de +100 contratos; si dos strikes saltan el mismo tamaño en menos de 2 s se
marcan como probable spread y salen de la lista principal.

Tape v2 (21-nov, manual "Flujo y Liquidez"):
  · 5 niveles de agresividad: C+ arriba del ask · C en el ask · M medio · V en el bid · V+ debajo del bid
    (el campo "lado" sigue siendo B/M/V para no romper el bot ni la revisión).
  · Bloques acumulados (órdenes divididas): piezas de +10 contratos del mismo contrato y lado que en 5 min
    suman +300 contratos en 2 o más piezas. El filtro de +100 tiraba justo esas órdenes partidas.
  · Volumen vs OI de apertura: si el volumen del día supera el OI con el que abrió el strike, por fuerza
    hay posiciones nuevas (la regla del OI del día siguiente no sirve en 0DTE: el contrato ya venció).
  · Liquidez cerca del precio (±15 pts): ancho bid/ask en % del medio, contra la base de la primera media
    hora, y desbalance de tamaños bid vs ask.

Publica tape.json en la rama live cada 30 s. Usa las mismas llaves que flightdeck_recolector.py
(debe estar en la misma carpeta). Solo librería estándar.

    python tape_schwab.py            # corre hasta las 15:01 CDMX
    python tape_schwab.py --prueba   # prueba local sin Schwab (datos sintéticos)
    python tape_schwab.py --validar  # login al streamer + suscripción al siguiente vencimiento, imprime y sale
"""
import base64, json, os, socket, ssl, struct, sys, time, traceback, urllib.parse, urllib.request
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flightdeck_recolector as R

MIN = int(sys.argv[sys.argv.index("--min") + 1]) if "--min" in sys.argv else 100   # contratos mínimos por print
MIN_ACUM = 10        # piezas mínimas que cuentan para un bloque acumulado
BLOQ_S = 300         # ventana del bloque (5 min)
BLOQ_N = 300         # contratos acumulados para declarar bloque
APERT_VOL = 500      # volumen mínimo para reportar volumen > OI
LIQ_PTS = 15         # zona de liquidez alrededor del precio
SPREAD_S = 2.0       # ventana para emparejar patas de un spread
N_STR = 30           # strikes por lado (5 pts) alrededor del precio
PUB_S = 30           # segundos entre publicaciones
RESUB_S = 600        # re-centrar strikes cada 10 min
CAMPOS = "0,2,3,4,8,9,16,17,18,20,21"   # símbolo, bid, ask, last, vol total, OI, bid size, ask size, last size, strike, tipo
NIVEL = {"C+": "Compra+", "C": "Compra", "M": "Medio", "V": "Venta", "V+": "Venta+"}


def nivel(px, bid, ask):
    """Agresividad según dónde se ejecutó contra el bid/ask del momento."""
    e = 0.001
    if not px:
        return "M"
    if ask and px > ask + e:
        return "C+"
    if ask and px >= ask - e:
        return "C"
    if bid and px < bid - e:
        return "V+"
    if bid and px <= bid + e:
        return "V"
    return "M"


# ---------------------------------------------------------------- websocket mínimo (RFC 6455, cliente)
class WS:
    def __init__(self, url):
        u = urllib.parse.urlparse(url)
        raw = socket.create_connection((u.hostname, u.port or 443), timeout=30)
        self.s = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1\r\nHost: {u.hostname}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.s.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            ch = self.s.recv(1)
            if not ch:
                raise ConnectionError("el streamer cerró en el handshake")
            head += ch
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.split(b"\r\n")[0].decode(errors="replace"))

    def _read(self, n):
        b = b""
        while len(b) < n:
            ch = self.s.recv(n - len(b))
            if not ch:
                raise ConnectionError("streamer desconectado")
            b += ch
        return b

    def send(self, text, op=1):
        data = text.encode() if isinstance(text, str) else text
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | op])
        hdr += bytes([0x80 | n]) if n < 126 else (bytes([0xFE]) + struct.pack(">H", n) if n < 65536 else bytes([0xFF]) + struct.pack(">Q", n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        buf = b""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            m = self._read(4) if b1 & 0x80 else None
            d = self._read(n)
            if m:
                d = bytes(x ^ m[i % 4] for i, x in enumerate(d))
            if op == 9:                      # ping → pong
                self.send(d, 10)
                continue
            if op == 8:
                raise ConnectionError("el streamer cerró la conexión")
            buf += d
            if b0 & 0x80:
                return buf.decode(errors="replace")


# ---------------------------------------------------------------- lógica del tape (sin red, se puede probar)
class Tape:
    def __init__(self):
        self.q = {}                         # símbolo → {bid, ask, last, vol, oi, bsz, asz, k, t}
        self.prints = deque(maxlen=400)
        self.pend = deque()                 # prints recientes para emparejar spreads
        self.acum = {}                      # (k, t, lado) → deque de piezas en la ventana del bloque
        self.bloques = deque(maxlen=60)
        self.agr = {"C": dict.fromkeys(NIVEL, 0), "P": dict.fromkeys(NIVEL, 0)}   # prima por nivel (prints de +MIN)
        self.liq_base = []                  # anchos medianos de la primera media hora (base del día)
        self.nbloq = 0

    def _bloque(self, p):
        """Acumula piezas de un mismo contrato y lado; declara bloque al pasar BLOQ_N en 2+ piezas."""
        if p["lado"] == "M":
            return None
        cola = self.acum.setdefault((p["k"], p["t"], p["lado"]), deque())
        while cola and p["ts"] - cola[0]["ts"] > BLOQ_S:
            cola.popleft()
        cola.append(p)
        n = sum(x["n"] for x in cola)
        if len(cola) < 2 or n < BLOQ_N:
            return None
        b = {"ts": p["ts"], "h_ini": cola[0]["h"], "h": p["h"], "k": p["k"], "t": p["t"], "lado": p["lado"],
             "n": n, "piezas": len(cola), "prem": sum(x["prem"] for x in cola),
             "px": round(sum(x["px"] * x["n"] for x in cola) / n, 2),
             "agr": "+" if any(x["agr"].endswith("+") for x in cola) else ""}
        cola.clear()
        self.bloques.append(b)
        self.nbloq += 1
        print(f'BLOQUE {b["h_ini"]}–{b["h"]} {b["k"]:g}{b["t"]} {b["n"]} en {b["piezas"]} piezas @ {b["px"]} '
              f'{"compra" if b["lado"] == "B" else "venta"}{b["agr"]}  ${b["prem"]:,}')
        return b

    def update(self, d, ts):
        sy = d.get("key") or d.get("0")
        if not sy:
            return None
        q = self.q.setdefault(sy, {"vol": None})
        for f, name in (("2", "bid"), ("3", "ask"), ("4", "last"), ("9", "oi"), ("16", "bsz"), ("17", "asz"), ("18", "lsz")):
            if f in d and d[f] is not None:
                q[name] = float(d[f])
        if "20" in d:
            q["k"] = float(d["20"])
        if "21" in d:
            q["t"] = str(d["21"])[:1].upper()
        if "k" not in q:                    # strike y tipo desde el símbolo OCC: SPXW  261111C07600000
            try:
                q["k"], q["t"] = int(sy[-8:]) / 1000, sy[-9]
            except ValueError:
                return None
        if "8" not in d:
            return None
        v = float(d["8"])
        prev, q["vol"] = q["vol"], v
        if prev is None or v - prev < MIN_ACUM:
            return None
        sz, px = int(v - prev), q.get("last") or 0
        bid, ask = q.get("bid") or 0, q.get("ask") or 0
        agr = nivel(px, bid, ask)
        lado = "B" if agr.startswith("C") else "V" if agr.startswith("V") else "M"
        p = {"ts": ts, "h": time.strftime("%H:%M:%S", time.localtime(ts)), "k": q["k"], "t": q["t"],
             "n": sz, "px": round(px, 2), "bid": bid, "ask": ask, "lado": lado, "agr": agr,
             "prem": round(sz * px * 100), "bloque": bool(q.get("lsz", 0) >= MIN), "spread": False}
        if sz < MIN:                        # pieza chica: solo cuenta para bloques acumulados
            self._bloque(p)
            return None
        while self.pend and ts - self.pend[0]["ts"] > SPREAD_S:
            self.pend.popleft()
        for o in self.pend:
            if o["n"] == sz and (o["k"], o["t"]) != (p["k"], p["t"]):
                if not o["spread"]:                     # ya había contado en la prima por nivel: se descuenta
                    self.agr[o["t"]][o["agr"]] -= o["prem"]
                o["spread"] = p["spread"] = True
        self.pend.append(p)
        self.prints.append(p)
        if not p["spread"]:
            self.agr[p["t"]][agr] += p["prem"]
            self._bloque(p)
        return p

    def apertura(self):
        """Strikes donde el volumen del día ya supera el OI de apertura: hay posiciones nuevas seguro."""
        out = []
        for q in self.q.values():
            vol, oi = q.get("vol") or 0, q.get("oi")
            if oi is None or vol < APERT_VOL or "k" not in q:
                continue
            if vol > oi:
                out.append({"k": q["k"], "t": q["t"], "vol": int(vol), "oi": int(oi),
                            "x": round(vol / oi, 1) if oi else None})
        return sorted(out, key=lambda r: -(r["x"] or 999))[:8]

    def liquidez(self, spot, hora_min=None):
        """Ancho bid/ask (% del medio) y desbalance de tamaños en ±LIQ_PTS del precio, por tipo."""
        if not spot:
            return None
        res = {}
        for t in ("C", "P"):
            anchos, bs, as_ = [], 0, 0
            for q in self.q.values():
                if q.get("t") != t or "k" not in q or abs(q["k"] - spot) > LIQ_PTS:
                    continue
                b, a = q.get("bid") or 0, q.get("ask") or 0
                if b > 0 and a > b:
                    anchos.append((a - b) / ((a + b) / 2) * 100)
                bs += q.get("bsz") or 0
                as_ += q.get("asz") or 0
            anchos.sort()
            res[t] = {"ancho": round(anchos[len(anchos) // 2], 1) if anchos else None,
                      "desb": round((bs - as_) / (bs + as_), 2) if bs + as_ else None, "n": len(anchos)}
        meds = [r["ancho"] for r in res.values() if r["ancho"] is not None]
        actual = sum(meds) / len(meds) if meds else None
        if actual is not None and hora_min is not None and 510 <= hora_min < 540:   # 8:30–9:00 CDMX
            self.liq_base.append(actual)
        base = sorted(self.liq_base)[len(self.liq_base) // 2] if len(self.liq_base) >= 5 else None
        res["ancho"] = round(actual, 1) if actual is not None else None
        res["base"] = round(base, 1) if base else None
        res["x"] = round(actual / base, 2) if actual and base else None
        return res

    def snapshot(self, spot=None, hora_min=None):
        limpios = [p for p in self.prints if not p["spread"]]
        res = {}
        for p in limpios:
            r = res.setdefault(f'{p["k"]:g}{p["t"]}', {"k": p["k"], "t": p["t"], "B": 0, "V": 0, "M": 0, "n": 0})
            r[p["lado"]] += p["prem"]
            r["n"] += p["n"]
        top = sorted(res.values(), key=lambda r: -(r["B"] + r["V"] + r["M"]))[:12]
        neto = lambda t, rs=None: sum(r["B"] - r["V"] for r in (rs if rs is not None else res.values()) if r["t"] == t)
        # 21-nov (#24): ITM de más de 30 pts = cierres, rolls y coberturas; fuera del neto y del gráfico "otm"
        otm = [r for r in res.values() if not spot or not ((r["t"] == "C" and r["k"] < spot - 30) or (r["t"] == "P" and r["k"] > spot + 30))]
        top_otm = sorted(otm, key=lambda r: -(r["B"] + r["V"] + r["M"]))[:12]
        return {"ts": int(time.time()), "spot": spot, "min": MIN,
                "strikes_otm": top_otm, "neto_otm": {"C": neto("C", otm), "P": neto("P", otm)},
                "prints": [{k: v for k, v in p.items() if k != "ts"} for p in list(limpios)[-60:]][::-1],
                "spreads": [{k: v for k, v in p.items() if k != "ts"} for p in self.prints if p["spread"]][-20:][::-1],
                "strikes": top, "neto": {"C": neto("C"), "P": neto("P")},
                # Tape v2
                "v": 2, "bloque_n": BLOQ_N, "bloque_s": BLOQ_S,
                "bloques": [{k: v for k, v in b.items() if k != "ts"} for b in self.bloques][-15:][::-1],
                "agr": self.agr, "apertura": self.apertura(), "liq": self.liquidez(spot, hora_min)}


# ---------------------------------------------------------------- Schwab
def occ(day, t, k):
    return f"SPXW  {day[2:4]}{day[5:7]}{day[8:10]}{t}{int(round(k * 1000)):08d}"


def strikes(c, day):
    spot = float(R.sget(c, "/quotes", symbols="$SPX")["$SPX"]["quote"]["lastPrice"])
    base = round(spot / 5) * 5
    ks = [base + 5 * i for i in range(-N_STR, N_STR + 1)]
    # calls desde 60 pts abajo del precio y puts hasta 60 pts arriba: lo cercano en ambos lados, lo lejano solo OTM
    syms = [occ(day, "C", k) for k in ks if k >= spot - 60] + [occ(day, "P", k) for k in ks if k <= spot + 60]
    return spot, syms


def streamer(c):
    req = urllib.request.Request("https://api.schwabapi.com/trader/v1/userPreference",
                                 headers={"Authorization": "Bearer " + R.access(c), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        j = json.loads(r.read())
    return (j.get("streamerInfo") or [j])[0]


def validar():
    import datetime as dt
    c = R.jload(R.SCHWAB)
    d = R.now().date() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    day = d.isoformat()
    print("1) preferencias del usuario (streamer)…")
    info = streamer(c)
    print("   url:", info.get("streamerSocketUrl"))
    print("2) conectando websocket…")
    ws = WS(info["streamerSocketUrl"])
    ids = {"SchwabClientCustomerId": info["schwabClientCustomerId"], "SchwabClientCorrelId": info["schwabClientCorrelId"]}
    ws.send(json.dumps({"requests": [dict(ids, service="ADMIN", command="LOGIN", requestid="1", parameters={
        "Authorization": R.access(c), "SchwabClientChannel": info["schwabClientChannel"],
        "SchwabClientFunctionId": info["schwabClientFunctionId"]})]}))
    print("3) login:", ws.recv()[:300])
    spot, syms = strikes(c, day)
    print(f"4) SPX {spot:.2f} · vencimiento {day} · {len(syms)} contratos, ej. {syms[0]!r}")
    ws.send(json.dumps({"requests": [dict(ids, service="LEVELONE_OPTIONS", command="SUBS", requestid="2",
                                          parameters={"keys": ",".join(syms), "fields": CAMPOS})]}))
    tape, n, t0 = Tape(), 0, time.time()
    while time.time() - t0 < 20 and n < 6:
        msg = json.loads(ws.recv())
        n += 1
        for r in msg.get("response", []):
            print("   respuesta:", json.dumps(r)[:300])
        for d_ in msg.get("data", []):
            cont = d_.get("content", [])
            print(f"   datos {d_.get('service')}: {len(cont)} contratos")
            for x in cont[:3]:
                print("     ", json.dumps(x)[:250])
            for x in cont:
                tape.update(x, time.time())
    con = sum(1 for q in tape.q.values() if q.get("bid") is not None or q.get("vol") is not None)
    print(f"\n5) contratos con datos: {con} de {len(syms)}")
    print("VALIDACIÓN OK" if con else "Sin datos de contratos: mándame foto de esto.")


def vence():
    """Vencimiento a seguir: hoy hasta las 15:00 CDMX; después, el siguiente día hábil (sesión nocturna de Cboe)."""
    import datetime as dt
    t = R.now()
    d = t.date() + dt.timedelta(days=1 if t.hour * 60 + t.minute >= 901 or t.weekday() >= 5 else 0)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.isoformat(), dt.datetime.fromisoformat(d.isoformat()).replace(hour=15, minute=1, tzinfo=R.MX)


def run():
    c = R.jload(R.SCHWAB)
    tok = R.gh_token()
    tape, sha = Tape(), {"v": None}
    day, fin = vence()
    print(f"Tape SPXW vencimiento {day} · prints de +{MIN} contratos · se apaga {fin:%d-%b %H:%M} CDMX")

    def publicar(spot):
        path = f"/repos/{R.REPO}/contents/tape.json"
        if sha["v"] is None:
            st, j = R.gh_req(tok, "GET", path + f"?ref={R.BRANCH}")
            sha["v"] = j.get("sha") if st == 200 else ""
        body = {"message": "tape " + R.now().strftime("%H:%M"), "branch": R.BRANCH,
                "content": base64.b64encode(json.dumps(tape.snapshot(spot, R.now().hour * 60 + R.now().minute),
                                                       separators=(",", ":")).encode()).decode()}
        if sha["v"]:
            body["sha"] = sha["v"]
        st, j = R.gh_req(tok, "PUT", path, body)
        if st in (200, 201):
            sha["v"] = j["content"]["sha"]
        else:
            sha["v"] = None
            print("GitHub", st, j.get("message"))

    while True:
        if R.now() >= fin:
            print("15:01 · fin del tape.")
            return
        try:
            # auditoría 12-nov: tras una reconexión, el primer TOTAL_VOLUME traía todo el volumen del hueco
            # y se contaba como un solo print gigante (neto de puts +$40 M). Se reinician las bases.
            for q in tape.q.values():
                q["vol"] = None
            tape.pend.clear()
            info = streamer(c)
            ws = WS(info["streamerSocketUrl"])
            ids = {"SchwabClientCustomerId": info["schwabClientCustomerId"], "SchwabClientCorrelId": info["schwabClientCorrelId"]}
            n = [0]

            def cmd(service, command, params):
                n[0] += 1
                ws.send(json.dumps({"requests": [dict(ids, service=service, command=command, requestid=str(n[0]), parameters=params)]}))

            cmd("ADMIN", "LOGIN", {"Authorization": R.access(c), "SchwabClientChannel": info["schwabClientChannel"],
                                   "SchwabClientFunctionId": info["schwabClientFunctionId"]})
            print("login:", ws.recv()[:160])
            spot, syms = strikes(c, day)
            cmd("LEVELONE_OPTIONS", "SUBS", {"keys": ",".join(syms), "fields": CAMPOS})
            print(f"suscrito a {len(syms)} contratos alrededor de {spot:.0f}")
            t_pub = t_sub = t_dat = time.time()
            n_dat = n_pr = 0
            while True:
                msg = json.loads(ws.recv())
                for d in msg.get("data", []):
                    if d.get("service") == "LEVELONE_OPTIONS":
                        t_dat = time.time()
                        n_dat += len(d.get("content", []))
                        for x in d.get("content", []):
                            p = tape.update(x, d.get("timestamp", time.time() * 1000) / 1000)
                            if p:
                                n_pr += 1
                            if p and not p["spread"]:
                                print(f'{p["h"]} {p["k"]:g}{p["t"]} {p["n"]:>5} @ {p["px"]:<6} {p["agr"]:<2}  ${p["prem"]:,}')
                for r in msg.get("response", []):
                    code = (r.get("content") or {}).get("code")
                    if code not in (0, None):
                        print("streamer:", r)
                for r in msg.get("notify", []):
                    if "heartbeat" not in r:
                        print("notify:", r)
                now_ = time.time()
                if now_ - t_pub >= PUB_S:
                    publicar(spot)
                    t_pub = now_
                    if R.now().second < 30:   # una línea por minuto: cuántas actualizaciones y prints llegaron
                        print(f"{R.now():%H:%M} · {n_dat} actualizaciones · {n_pr} prints · spot {spot:.0f} · {len(syms)} contratos")
                        n_dat = n_pr = 0
                # vigilante (16-nov: publicó todo el día sin un print nuevo): en horario de mercado,
                # 3 min sin datos de opciones = la suscripción murió → reconectar
                hm = R.now().hour * 60 + R.now().minute
                if 510 <= hm < 900 and now_ - t_dat > 180:
                    raise ConnectionError("3 min sin datos de LEVELONE_OPTIONS: reconecto y vuelvo a suscribir")
                    if R.now() >= fin:
                        return
                if now_ - t_sub >= RESUB_S:
                    spot, syms = strikes(c, day)
                    cmd("LEVELONE_OPTIONS", "ADD", {"keys": ",".join(syms), "fields": CAMPOS})
                    t_sub = now_
        except KeyboardInterrupt:
            return
        except Exception:
            traceback.print_exc()
            print("reconectando en 10 s…")
            time.sleep(10)


def prueba():
    tp = Tape()
    s = lambda k, t: occ("2026-11-12", t, k)
    t0 = 1000.0
    for sy in (s(7600, "C"), s(7610, "C"), s(7580, "P")):
        tp.update({"key": sy, "2": 1.0, "3": 1.2, "8": 500}, t0)
    tp.update({"key": s(7600, "C"), "4": 1.2, "8": 750, "18": 250}, t0 + 1)       # 250 a ask → B
    tp.update({"key": s(7580, "P"), "4": 1.0, "8": 640}, t0 + 5)                  # 140 a bid → V
    tp.update({"key": s(7600, "C"), "4": 1.1, "8": 1050}, t0 + 9)                 # 300 …
    tp.update({"key": s(7610, "C"), "4": 0.6, "8": 800}, t0 + 9.5)                # … 300 en otro strike = spread
    tp.update({"key": s(7580, "P"), "4": 1.0, "8": 690}, t0 + 12)                 # 50 < MIN → ignora
    snap = tp.snapshot(7598)
    print(json.dumps(snap, indent=1, ensure_ascii=False)[:1500])
    assert [p["lado"] for p in snap["prints"]] == ["V", "B"], snap["prints"]
    assert len(snap["spreads"]) == 2 and snap["neto"]["C"] == 30000 and snap["neto"]["P"] == -14000
    # Tape v2: niveles, bloque acumulado de piezas chicas, volumen > OI y liquidez
    assert [p["agr"] for p in snap["prints"]] == ["V", "C"], snap["prints"]
    assert nivel(1.25, 1.0, 1.2) == "C+" and nivel(0.95, 1.0, 1.2) == "V+" and nivel(1.1, 1.0, 1.2) == "M"
    sy = s(7595, "P")
    tp.update({"key": sy, "2": 2.0, "3": 2.1, "9": 400, "16": 50, "17": 10, "8": 0}, t0 + 20)
    vol = 0
    for i in range(4):                                   # 4 piezas de 90 al ask en 4 min → bloque de 360
        vol += 90
        tp.update({"key": sy, "4": 2.1, "8": vol}, t0 + 30 + 60 * i)
    tp.update({"key": sy, "8": vol + 200, "4": 2.05}, t0 + 400)   # 200 al medio: print, no suma al bloque
    snap = tp.snapshot(7598, 520)
    assert len(snap["bloques"]) == 1 and snap["bloques"][0]["n"] == 360 and snap["bloques"][0]["piezas"] == 4, snap["bloques"]
    assert snap["apertura"] and snap["apertura"][0]["k"] == 7595 and snap["apertura"][0]["vol"] == 560, snap["apertura"]
    assert snap["liq"]["P"]["desb"] == round((50 - 10) / 60, 2) and snap["liq"]["P"]["ancho"] is not None, snap["liq"]
    print(json.dumps({k: snap[k] for k in ("bloques", "apertura", "liq", "agr")}, ensure_ascii=False))
    print("\nOK prueba")


class _Tee:
    """Copia todo lo que se imprime a historial/tape_AAAA-MM-DD.log (auditoría 16-nov: la ventana se cerró y no quedó el error)."""
    def __init__(self, *s):
        self.s = s
    def write(self, x):
        for f in self.s:
            try:
                f.write(x); f.flush()
            except Exception:
                pass
    def flush(self):
        pass


def _log_a_archivo():
    try:
        d = R.HIST; d.mkdir(exist_ok=True)
        f = open(d / f"tape_{R.now():%Y-%m-%d}.log", "a", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, f)
        sys.stderr = _Tee(sys.__stderr__, f)
        print(f"\n==== tape {R.now():%Y-%m-%d %H:%M:%S} ====")
    except Exception as e:
        print("sin log a archivo:", e)


if __name__ == "__main__":
    if not ("--prueba" in sys.argv or "--validar" in sys.argv):
        _log_a_archivo()
    if "--prueba" in sys.argv:
        prueba()
    elif "--validar" in sys.argv:
        validar()
    else:
        run()
