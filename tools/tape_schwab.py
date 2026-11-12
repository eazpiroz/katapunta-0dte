"""Tape aproximado de SPX 0DTE con el streamer de Schwab (pendiente #14).

Schwab no da time & sales de opciones. Este script se suscribe a LEVELONE_OPTIONS de ~60
contratos SPXW 0DTE cerca del precio y reconstruye los prints a partir de los saltos de
TOTAL_VOLUME, clasificándolos contra el bid/ask del momento:
    precio >= ask  → B (compra agresiva)      precio <= bid → V (venta agresiva)      otro → M (medio)
Filtro: solo saltos de +100 contratos; si dos strikes saltan el mismo tamaño en menos de 2 s se
marcan como probable spread y salen de la lista principal.

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
SPREAD_S = 2.0       # ventana para emparejar patas de un spread
N_STR = 30           # strikes por lado (5 pts) alrededor del precio
PUB_S = 30           # segundos entre publicaciones
RESUB_S = 600        # re-centrar strikes cada 10 min
CAMPOS = "0,2,3,4,8,16,17,18,20,21"   # símbolo, bid, ask, last, vol total, bid size, ask size, last size, strike, tipo


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
        self.q = {}                         # símbolo → {bid, ask, last, vol, k, t}
        self.prints = deque(maxlen=400)
        self.pend = deque()                 # prints recientes para emparejar spreads

    def update(self, d, ts):
        sy = d.get("key") or d.get("0")
        if not sy:
            return None
        q = self.q.setdefault(sy, {"vol": None})
        for f, name in (("2", "bid"), ("3", "ask"), ("4", "last"), ("18", "lsz")):
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
        if prev is None or v - prev < MIN:
            return None
        sz, px = int(v - prev), q.get("last") or 0
        bid, ask = q.get("bid") or 0, q.get("ask") or 0
        lado = "B" if ask and px >= ask else "V" if bid and px <= bid else "M"
        p = {"ts": ts, "h": time.strftime("%H:%M:%S", time.localtime(ts)), "k": q["k"], "t": q["t"],
             "n": sz, "px": round(px, 2), "bid": bid, "ask": ask, "lado": lado,
             "prem": round(sz * px * 100), "bloque": bool(q.get("lsz", 0) >= MIN), "spread": False}
        while self.pend and ts - self.pend[0]["ts"] > SPREAD_S:
            self.pend.popleft()
        for o in self.pend:
            if o["n"] == sz and (o["k"], o["t"]) != (p["k"], p["t"]):
                o["spread"] = p["spread"] = True
        self.pend.append(p)
        self.prints.append(p)
        return p

    def snapshot(self, spot=None):
        limpios = [p for p in self.prints if not p["spread"]]
        res = {}
        for p in limpios:
            r = res.setdefault(f'{p["k"]:g}{p["t"]}', {"k": p["k"], "t": p["t"], "B": 0, "V": 0, "M": 0, "n": 0})
            r[p["lado"]] += p["prem"]
            r["n"] += p["n"]
        top = sorted(res.values(), key=lambda r: -(r["B"] + r["V"] + r["M"]))[:12]
        neto = lambda t: sum(r["B"] - r["V"] for r in res.values() if r["t"] == t)
        return {"ts": int(time.time()), "spot": spot, "min": MIN,
                "prints": [{k: v for k, v in p.items() if k != "ts"} for p in list(limpios)[-60:]][::-1],
                "spreads": [{k: v for k, v in p.items() if k != "ts"} for p in self.prints if p["spread"]][-20:][::-1],
                "strikes": top, "neto": {"C": neto("C"), "P": neto("P")}}


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
                "content": base64.b64encode(json.dumps(tape.snapshot(spot), separators=(",", ":")).encode()).decode()}
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
            t_pub = t_sub = time.time()
            while True:
                msg = json.loads(ws.recv())
                for d in msg.get("data", []):
                    if d.get("service") == "LEVELONE_OPTIONS":
                        for x in d.get("content", []):
                            p = tape.update(x, d.get("timestamp", time.time() * 1000) / 1000)
                            if p and not p["spread"]:
                                print(f'{p["h"]} {p["k"]:g}{p["t"]} {p["n"]:>5} @ {p["px"]:<6} {p["lado"]}  ${p["prem"]:,}')
                for r in msg.get("response", []):
                    code = (r.get("content") or {}).get("code")
                    if code not in (0, None):
                        print("streamer:", r)
                now_ = time.time()
                if now_ - t_pub >= PUB_S:
                    publicar(spot)
                    t_pub = now_
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
    print("\nOK prueba")


if __name__ == "__main__":
    if "--prueba" in sys.argv:
        prueba()
    elif "--validar" in sys.argv:
        validar()
    else:
        run()
