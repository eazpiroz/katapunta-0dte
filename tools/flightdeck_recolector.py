#!/usr/bin/env python3
"""
Katapunta Flight Deck · recolector automático (Schwab Market Data → 0dte.katapunta.com)

Cada minuto, de la hora en que lo abras hasta las 15:01 CDMX:
  - lee de Schwab cotizaciones, velas de 5 min y la cadena 0DTE de SPX, SPY y QQQ
  - calcula straddle, rango restante, GEX por strike, muros, techo/piso/PIN e iron condor de referencia
  - toma los majors y el zero γ de la última lectura manual (data.json), si existe; si no, los estima
  - estima GEX/DEX/charm/vanna (0DTE y próximos vencimientos), zero γ por malla de precios, bordes delta 10/15
  - publica auto.json en la rama "live" del repo; el tablero lo lee solo

Llaves: usa las de Schwab que ya guardó schwab_prueba.py y te pide UNA vez un token de GitHub.
Todo se guarda solo en %USERPROFILE%\\.katapunta\\ — nunca en el repo.
Uso:  python flightdeck_recolector.py          (déjalo abierto durante la sesión; Ctrl+C para salir)
      python flightdeck_recolector.py --una    (una sola vuelta, para probar)
Solo Python 3.9+, sin librerías externas.
"""
import base64, datetime as dt, getpass, json, math, os, sys, time, traceback
import urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path

DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".katapunta"
SCHWAB = DIR / "schwab.json"
GH = DIR / "github.json"
DIA = DIR / "dia.json"
REPO = "eazpiroz/katapunta-0dte"
BRANCH = "live"
REDIRECT = "https://127.0.0.1"
API = "https://api.schwabapi.com/marketdata/v1"
MX = dt.timezone(dt.timedelta(hours=-6))            # CDMX, sin horario de verano
TICKERS = {"SPX": "$SPX", "SPY": "SPY", "QQQ": "QQQ"}
STEP = {"SPX": 10, "SPY": 1, "QQQ": 1}               # paso del perfil
SPAN = {"SPX": 150, "SPY": 14, "QQQ": 14}            # strikes alrededor del precio
TOL = {"SPX": 10, "SPY": 1, "QQQ": 1}                # tolerancia de agrupación
WING = {"SPX": 10, "SPY": 1, "QQQ": 1}               # ancho de alas del IC de referencia
GTOL = {"SPX": 3, "SPY": 0.3, "QQQ": 0.3}             # banda del zero γ para decir positiva/negativa
SKEW = (1.6, 0.9)                                     # IC ajustado: lado débil 1.6× straddle, lado fuerte 0.9× (backtest 2-nov)
HIST = Path(__file__).resolve().parent / "historial"  # una línea por minuto, para backtest (sin llaves)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def now():
    return dt.datetime.now(MX)


def jload(p):
    return json.loads(p.read_text()) if p.exists() else {}


def jsave(p, d):
    DIR.mkdir(exist_ok=True)
    p.write_text(json.dumps(d, indent=1))


# ---------------------------------------------------------------- Schwab OAuth
def token_request(c, form):
    basic = base64.b64encode(f"{c['key']}:{c['secret']}".encode()).decode()
    req = urllib.request.Request("https://api.schwabapi.com/v1/oauth/token",
                                 data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Authorization": "Basic " + basic,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        t = json.loads(r.read())
    c["access"] = t["access_token"]
    c["access_exp"] = time.time() + int(t.get("expires_in", 1800)) - 60
    if "refresh_token" in t:
        c["refresh"] = t["refresh_token"]
        if form.get("grant_type") == "authorization_code":
            c["refresh_exp"] = time.time() + 7 * 24 * 3600 - 3600
    jsave(SCHWAB, c)


def login(c):
    url = "https://api.schwabapi.com/v1/oauth/authorize?" + urllib.parse.urlencode({"client_id": c["key"], "redirect_uri": REDIRECT})
    print("\nHay que renovar el permiso de Schwab (cada 7 días).")
    input("Presiona Enter para abrir Schwab, inicia sesión y pega aquí la dirección 127.0.0.1 completa... ")
    webbrowser.open(url)
    back = input("Dirección: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    token_request(c, {"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT})
    print("Permiso renovado.\n")


def access(c):
    if c.get("access") and time.time() < c.get("access_exp", 0):
        return c["access"]
    if c.get("refresh") and time.time() < c.get("refresh_exp", 0):
        try:
            token_request(c, {"grant_type": "refresh_token", "refresh_token": c["refresh"]})
            return c["access"]
        except Exception as e:
            print("No se pudo renovar:", e)
    login(c)
    return c["access"]


def sget(c, path, **params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access(c), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- GitHub
def gh_req(tok, method, path, body=None):
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "katapunta-flightdeck"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def gh_token():
    g = jload(GH)
    if not g.get("token"):
        print("\nNecesito un token de GitHub con permiso de escribir solo en el repo katapunta-0dte.")
        g["token"] = getpass.getpass("Token de GitHub (no se verá al pegar): ").strip()
        jsave(GH, g)
    return g["token"]


def gh_manual(tok):
    """Última lectura manual (data.json en main): majors y zero γ."""
    st, j = gh_req(tok, "GET", f"/repos/{REPO}/contents/data.json?ref=main")
    if st != 200:
        return {}
    try:
        return json.loads(base64.b64decode(j["content"]))
    except Exception:
        return {}


_sha = {"v": None}


def gh_put(tok, content):
    path = f"/repos/{REPO}/contents/auto.json"
    if _sha["v"] is None:
        st, j = gh_req(tok, "GET", path + f"?ref={BRANCH}")
        _sha["v"] = j.get("sha") if st == 200 else ""
    body = {"message": "auto " + now().strftime("%H:%M"), "branch": BRANCH,
            "content": base64.b64encode(content.encode()).decode()}
    if _sha["v"]:
        body["sha"] = _sha["v"]
    st, j = gh_req(tok, "PUT", path, body)
    if st in (200, 201):
        _sha["v"] = j["content"]["sha"]
        return True
    if st in (409, 422):           # sha viejo: reintenta una vez
        _sha["v"] = None
        return gh_put(tok, content) if body.get("sha") else False
    print("GitHub respondió", st, j.get("message"))
    return False


def gh_put_hist(tok, day):
    """Sube el historial del día (gzip) a la rama live: historial/AAAA-MM-DD.jsonl.gz, para la auditoría diaria."""
    f = HIST / f"{day}.jsonl"
    if not f.exists():
        return False
    import gzip
    path = f"/repos/{REPO}/contents/historial/{day}.jsonl.gz"
    st, j = gh_req(tok, "GET", path + f"?ref={BRANCH}")
    body = {"message": "historial " + day, "branch": BRANCH,
            "content": base64.b64encode(gzip.compress(f.read_bytes())).decode()}
    if st == 200 and j.get("sha"):
        body["sha"] = j["sha"]
    st, j = gh_req(tok, "PUT", path, body)
    print(f"historial {day} → GitHub: {'ok' if st in (200, 201) else st}")
    return st in (200, 201)


# ---------------------------------------------------------------- cálculos
def ema(xs, n):
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def resample(bars, mins):
    """Agrupa velas de 5 min en velas de 'mins' alineadas a la apertura (8:30 CDMX)."""
    out, key = [], None
    for b in bars:
        t = dt.datetime.fromtimestamp(b["datetime"] / 1000, MX)
        k = (t.date(), ((t.hour * 60 + t.minute) - 510) // mins)
        if k != key:
            out.append(dict(b)); key = k
        else:
            o = out[-1]; o["high"] = max(o["high"], b["high"]); o["low"] = min(o["low"], b["low"])
            o["close"] = b["close"]; o["volume"] = o.get("volume", 0) + b.get("volume", 0)
    return out


def trend(closes, px, tolpct):
    e20, e50 = ema(closes, 20), ema(closes, 50)
    if e20 is None or e50 is None:
        return 0
    if abs(px - e20) <= e20 * tolpct:
        return 0
    pos = 1 if px > e20 else -1
    tr = 1 if e20 > e50 else -1
    return pos if pos == tr else 0


def emas(xs, n):
    if not xs:
        return []
    k = 2 / (n + 1); e = xs[0]; out = []
    for x in xs:
        e = x * k + e * (1 - k); out.append(e)
    return out


def trend2(closes, px, vw):
    """Regla nueva (en prueba): alcista si precio > EMA9 > SMA20 y precio > VWAP; bajista al revés."""
    xs = closes + [px]
    if len(xs) < 21:
        return 0
    e9, s20 = emas(xs, 9)[-1], sum(xs[-20:]) / 20
    if px > e9 > s20 and (vw is None or px > vw):
        return 1
    if px < e9 < s20 and (vw is None or px < vw):
        return -1
    return 0


def momentum(closes, px, vw):
    """Momentum direccional (estudio del socio): alcista si EMA9 > EMA21 con ambas subiendo (3 velas), precio > SMA20 (media de Bollinger),
    RSI14 > 50, histograma MACD > 0 y precio > VWAP; bajista al revés. Devuelve (estado, acelerando)."""
    xs = (closes + [px])[-200:]
    if len(xs) < 40:
        return 0, False
    e9, e21 = emas(xs, 9), emas(xs, 21)
    s20 = sum(xs[-20:]) / 20
    g = l = 0.0
    for a, b in zip(xs[-15:-1], xs[-14:]):
        g += max(b - a, 0); l += max(a - b, 0)
    rsi = 100 if l == 0 else 100 - 100 / (1 + g / l)
    macd = [a - b for a, b in zip(emas(xs, 12), emas(xs, 26))]
    sig = emas(macd, 9)
    h = [a - b for a, b in zip(macd, sig)]
    up = e9[-1] > e21[-1] and e9[-1] > e9[-4] and e21[-1] > e21[-4] and px > s20 and rsi > 50 and h[-1] > 0 and px > vw
    dn = e9[-1] < e21[-1] and e9[-1] < e9[-4] and e21[-1] < e21[-4] and px < s20 and rsi < 50 and h[-1] < 0 and px < vw
    st_ = 1 if up else -1 if dn else 0
    acc = bool(st_ and ((h[-1] > h[-2]) if st_ > 0 else (h[-1] < h[-2])))
    return st_, acc


def vwap(bars):
    """VWAP del día con precio típico; si el índice no trae volumen, promedio simple del precio típico."""
    if not bars:
        return None
    tp = [(b["high"] + b["low"] + b["close"]) / 3 for b in bars]
    v = [b.get("volume", 0) or 0 for b in bars]
    return sum(a * w for a, w in zip(tp, v)) / sum(v) if sum(v) else sum(tp) / len(tp)


def fuerza(bars5, px, edges):
    """En un extremo (borde del rango, techo o piso): acelerado si la EMA9 de 5m sigue empujando hacia él, frenado si se aplanó o volteó."""
    if len(bars5) < 16:
        return None
    up = [(abs(px - v), lab) for v, lab in edges["up"] if v and abs(px - v) <= edges["tol"] or (v and px > v)]
    dn = [(abs(px - v), lab) for v, lab in edges["dn"] if v and abs(px - v) <= edges["tol"] or (v and px < v)]
    if not up and not dn:
        return None
    side, lab = (1, min(up)[1]) if up and (not dn or min(up) <= min(dn)) else (-1, min(dn)[1])
    cs = [b["close"] for b in bars5] + [px]
    e = emas(cs, 9)
    tr = [max(b["high"], p["close"]) - min(b["low"], p["close"]) for p, b in zip(bars5[-15:-1], bars5[-14:])]
    atr = sum(tr) / len(tr) or 1
    slope = (e[-1] - e[-4]) / 3 / atr * side
    est = "acelerado" if slope >= 0.3 else "frenado"
    return f"En extremo ({lab}): llega {est} · pendiente EMA 9 de 5m {slope:+.2f} ATR."


def history(c, sym):
    end = int(time.time() * 1000)
    j = sget(c, "/pricehistory", symbol=sym, periodType="day", frequencyType="minute", frequency=5,
             startDate=end - 14 * 24 * 3600 * 1000, endDate=end, needExtendedHoursData="false")
    return j.get("candles", [])


def mid(o):
    if not o:
        return float("nan")
    return (o.get("bid", 0) + o.get("ask", 0)) / 2


# ---------------------------------------------------------------- griegas y exposición (según bibliografía GEX del socio)
# Convención de inventario (SUPUESTO, no posición observada): el dealer queda largo calls (q = +OI) y corto puts (q = −OI).
# GEX_1% = Σ q·100·γ·S²·0.01 · Black-Scholes con r = q = 0, IV de cada contrato, T en años de calendario hasta las 15:00 CDMX.
NDAYS = 10                                           # vencimientos que se leen además del 0DTE (días de calendario)


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, side):
    """delta, gamma, vanna (∂Δ/∂σ), charm (∂Δ/∂t por año) — r = q = 0."""
    if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0, 0.0, 0.0
    sq = iv * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / sq
    d2 = d1 - sq
    pd = npdf(d1)
    delta = ncdf(d1) - (0 if side == "c" else 1)
    return delta, pd / (S * sq), -pd * d2 / iv, pd * d2 / (2 * T)


def chain(c, sym, S, today, tk):
    """Devuelve (m0, contratos): m0 = cadena 0DTE por strike {k: {"c": o, "p": o}} y la lista de contratos de hasta NDAYS días."""
    hasta = (dt.date.fromisoformat(today) + dt.timedelta(days=NDAYS)).isoformat()
    j = sget(c, "/chains", symbol=sym, contractType="ALL", strikeCount=80 if tk == "SPX" else 40,
             includeUnderlyingQuote="true", fromDate=today, toDate=hasta)
    m0, out = {}, []
    t_now = now()
    for side, key in (("c", "callExpDateMap"), ("p", "putExpDateMap")):
        for exp, ks in j.get(key, {}).items():
            ed = exp.split(":")[0]
            close = dt.datetime.fromisoformat(ed).replace(hour=15, tzinfo=MX)
            T = max(1, (close - t_now).total_seconds() / 60) / (365 * 1440)
            for k, lst in ks.items():
                o = lst[0]
                sy = str(o.get("symbol", ""))
                if tk == "SPX" and ed == today and not sy.startswith("SPXW"):
                    continue                          # el SPX mensual AM ya liquidó en la mañana
                iv = o.get("volatility")
                try:
                    iv = float(iv) / 100
                except (TypeError, ValueError):
                    iv = 0
                if not (0.01 < iv < 3):
                    iv = 0
                out.append({"k": float(k), "s": side, "d0": ed == today, "T": T, "iv": iv,
                            "oi": o.get("openInterest", 0) or 0, "vol": o.get("totalVolume", 0) or 0,
                            "sd": o.get("delta"), "o": o})
                if ed == today:
                    m0.setdefault(float(k), {})[side] = o
    # IV faltante → IV del strike válido más cercano del mismo vencimiento y lado
    for x in out:
        if not x["iv"]:
            pares = [y for y in out if y["T"] == x["T"] and y["s"] == x["s"] and y["iv"]]
            if pares:
                x["iv"] = min(pares, key=lambda y: abs(y["k"] - x["k"]))["iv"]
    return m0, [x for x in out if x["iv"]]


def exposure(cons, S, peso="oi"):
    """Por strike: GEX 1% ($MM), y totales GEX, DEX, flujo charm por hora y flujo vanna por −1 pto de IV ($MM)."""
    gk, tot = {}, {"gex": 0.0, "dex": 0.0, "charm": 0.0, "vanna": 0.0, "gross": 0.0}
    for x in cons:
        n = x[peso]
        if not n:
            continue
        q = n if x["s"] == "c" else -n               # supuesto de inventario dealer
        d, g, va, ch = bs(S, x["k"], x["T"], x["iv"], x["s"])
        gex = q * 100 * g * S * S * 0.01 / 1e6
        gk[x["k"]] = gk.get(x["k"], 0) + gex
        tot["gex"] += gex; tot["gross"] += abs(gex)
        tot["dex"] += n * 100 * d * S / 1e9          # DEX del interés abierto (calls +, puts −), miles de millones
        tot["charm"] += -q * 100 * ch / (365 * 24) * S / 1e6   # + = dealers compran por hora (soporte), − = venden
        tot["vanna"] += -q * 100 * va * (-0.01) * S / 1e6      # flujo si la IV baja 1 punto
    return gk, tot


def flips(cons, S, tk):
    """Zero γ: recalcula la gamma de TODOS los contratos en una malla de precios hipotéticos y busca cambios de signo."""
    span, step = (0.03 * S, 2.0) if tk == "SPX" else (0.03 * S, 0.2)
    grid = [S - span + i * step for i in range(int(2 * span / step) + 1)]
    act = [x for x in cons if x["oi"]]
    vals = []
    for s in grid:
        v = 0.0
        for x in act:
            g = bs(s, x["k"], x["T"], x["iv"], x["s"])[1]
            v += (x["oi"] if x["s"] == "c" else -x["oi"]) * g * s * s
        vals.append(v)
    roots = []
    for i in range(1, len(grid)):
        if vals[i - 1] == 0 or vals[i - 1] * vals[i] < 0:
            a, b = grid[i - 1], grid[i]
            roots.append(round(a + (b - a) * abs(vals[i - 1]) / (abs(vals[i - 1]) + abs(vals[i])), 2))
    return roots


def strike_delta(m0, S, T, target, side):
    """Strike 0DTE cuyo delta está más cerca del objetivo (usa el delta de Schwab; si falta, Black-Scholes)."""
    best = None
    for k, d in m0.items():
        o = d.get(side)
        if not o:
            continue
        de = o.get("delta")
        try:
            de = float(de)
        except (TypeError, ValueError):
            de = None
        if de is None or de != de or abs(de) > 1:
            iv = (o.get("volatility") or 0) / 100
            de = bs(S, k, T, iv, side)[0] if iv > 0 else None
        if de is None:
            continue
        if best is None or abs(abs(de) - target) < abs(abs(best[1]) - target):
            best = (k, de)
    return best[0] if best else None


FAM = {"major +": "γ", "major −": "γ", "muro call": "γ", "muro put": "γ", "nodo γ+": "γ",
       "major + vol": "flujo", "major − vol": "flujo", "vol calls": "flujo", "vol puts": "flujo"}


def clusters(cands, tol):
    """cands: [(nivel, fuente)] → grupos [(centro, n_familias, n_fuentes)]. Familias: gamma/OI, flujo del día, técnica."""
    cands = sorted([x for x in cands if x[0] is not None])
    groups = []
    for v, src in cands:
        if groups and v - groups[-1]["lo"] <= tol:
            g = groups[-1]; g["vals"].append(v); g["srcs"].add(src)
        else:
            groups.append({"lo": v, "vals": [v], "srcs": {src}})
    out = []
    for g in groups:
        center = max(set(g["vals"]), key=g["vals"].count) if len(g["vals"]) > 1 else g["vals"][0]
        fams = {FAM.get(s, "técnica") for s in g["srcs"]}
        out.append((center, len(fams), len(g["srcs"])))
    return out


def fmt_pct(x):
    return ("+" if x >= 0 else "−") + f"{abs(x):.2f}%"


def build_ticker(c, tk, q, manual, today, state, vixinfo, macro):
    sym = TICKERS[tk]
    qq = q.get(sym, {}).get("quote", {})
    px = qq.get("lastPrice") or qq.get("mark")
    prev = qq.get("closePrice")
    chg = qq.get("netPercentChange") or ((px / prev - 1) * 100 if prev else 0)
    t_now = now()
    mins = t_now.hour * 60 + t_now.minute

    # velas y tendencia
    bars = history(c, sym)
    cut = (t_now - dt.timedelta(minutes=5)).timestamp() * 1000
    closed = [b for b in bars if b["datetime"] <= cut]
    if bars:
        state["_ultima_" + tk] = dt.datetime.fromtimestamp(bars[-1]["datetime"] / 1000, MX).strftime("%Y-%m-%d %H:%M")
    todays = [b for b in closed if dt.datetime.fromtimestamp(b["datetime"] / 1000, MX).date().isoformat() == today]
    before = [b for b in closed if dt.datetime.fromtimestamp(b["datetime"] / 1000, MX).date().isoformat() < today]
    c5 = [b["close"] for b in closed]
    c15 = [b["close"] for b in resample(closed, 15)]
    c60 = [b["close"] for b in resample(closed, 60)]
    tf = [trend(c5, px, 0.0005), trend(c15, px, 0.0005), trend(c60, px, 0.0005)]
    vw = vwap(todays) or px
    tf2 = [trend2(c5, px, vw), trend2(c15, px, vw), trend2(c60, px, vw)]
    mo = [momentum(c5, px, vw), momentum(c15, px, vw), momentum(c60, px, vw)]
    mom = [x[0] for x in mo]
    momA = mom[0] if mom[0] == mom[1] == mom[2] != 0 else 0
    tech = {"5m SMA200": sma(c5, 200), "15m SMA100": sma(c15, 100), "15m SMA200": sma(c15, 200),
            "15m EMA100": ema(c15, 100), "15m EMA200": ema(c15, 200), "VWAP": vw if todays else None}

    # cadena: 0DTE + vencimientos de los próximos días
    m, cons = chain(c, sym, px, today, tk)
    cons0 = [x for x in cons if x["d0"]]
    T0 = cons0[0]["T"] if cons0 else 1 / (365 * 1440)
    ks = sorted(k for k in m if "c" in m[k] and "p" in m[k])
    atm = min(ks, key=lambda k: abs(k - px)) if ks else None
    strad = round(mid(m[atm]["c"]) + mid(m[atm]["p"]), 2) if atm else None
    em_key = "em_" + tk
    if mins >= 510 and state.get(em_key + "_d") != today and strad:
        state[em_key] = [round(px - strad, 2), round(px + strad, 2), strad]
        state[em_key + "_d"] = today
        jsave(DIA, state)
    em = state.get(em_key) if state.get(em_key + "_d") == today else [round(px - strad, 2), round(px + strad, 2), strad]
    rem = [round(px - strad, 1), round(px + strad, 1)] if strad else None
    bands = [[round(px - f * strad, 1), round(px + f * strad, 1)] for f in (1.5, 2)] if strad else None

    # exposición: 0DTE por OI, 0DTE por volumen, total por OI
    gk0, t0 = exposure(cons0, px, "oi")
    gkv, tv = exposure(cons0, px, "vol")
    _, tT = exposure(cons, px, "oi")

    rows, cw, pw, cv, pv = [], (0, 0), (0, 0), (0, 0), (0, 0)
    for k in sorted(m):
        if abs(k - px) > SPAN[tk]:
            continue
        cc, pp = m[k].get("c", {}), m[k].get("p", {})
        cOI, pOI = cc.get("openInterest", 0) or 0, pp.get("openInterest", 0) or 0
        cV, pV = cc.get("totalVolume", 0) or 0, pp.get("totalVolume", 0) or 0
        if k > px and cOI > cw[1]: cw = (k, cOI)
        if k < px and pOI > pw[1]: pw = (k, pOI)
        if cV > cv[1]: cv = (k, cV)
        if pV > pv[1]: pv = (k, pV)
        if round(k) % STEP[tk] == 0 and float(k).is_integer():
            rows.append([int(k) if tk == "SPX" else k, cOI, pOI, cV, pV, round(gk0.get(k, 0), 1), round(gkv.get(k, 0), 1)])

    near = {k: g for k, g in gk0.items() if abs(k - px) <= SPAN[tk]}
    nearv = {k: g for k, g in gkv.items() if abs(k - px) <= SPAN[tk]}
    pick = lambda d, f: (f(d, key=d.get) if d else None)
    mp_e = pick({k: g for k, g in near.items() if g > 0}, max)
    mn_e = pick({k: g for k, g in near.items() if g < 0}, min)
    mpv = pick({k: g for k, g in nearv.items() if g > 0}, max)
    mnv = pick({k: g for k, g in nearv.items() if g < 0}, min)
    mvc = max(near, key=lambda k: abs(near[k])) if near else None
    fl0, flT = flips(cons0, px, tk), flips(cons, px, tk)
    zg_e = min(fl0, key=lambda r: abs(r - px)) if fl0 else None

    # majors y zero γ: de la lectura manual (perfil γ) si es de hoy; si no, estimación propia
    mr = None
    if manual.get("date") == today and manual.get(tk, {}).get("reads"):
        mr = manual[tk]["reads"][-1]
    if mr:
        zg, mp, mn, gt = mr["zg"], mr["mp"], mr["mn"], mr["t"]
    else:
        zg = zg_e if zg_e is not None else px
        mp, mn, gt = mp_e or px, mn_e or px, None

    # régimen con incertidumbre: signo 0DTE vs total y magnitud frente a la exposición bruta
    sg = lambda v: 1 if v > 0 else -1 if v < 0 else 0
    inc = (not fl0 and abs(t0["gex"]) < 0.05 * (t0["gross"] or 1)) or abs(px - zg) <= GTOL[tk]
    reg = {"d0": sg(t0["gex"]), "tot": sg(tT["gex"]), "inc": bool(inc), "mixto": sg(t0["gex"]) != sg(tT["gex"])}

    cands = [(mp, "major +"), (mn, "major −"), (cw[0] or None, "muro call"), (pw[0] or None, "muro put"),
             (mpv, "major + vol"), (mnv, "major − vol"), (cv[0] or None, "vol calls"), (pv[0] or None, "vol puts")]
    if mvc is not None and near[mvc] > 0:
        cands.append((mvc, "nodo γ+"))           # el nodo negativo acelera/expulsa: no cuenta como techo ni piso
    cands += [(v, n) for n, v in tech.items() if v]
    groups = clusters(cands, TOL[tk])
    above = [g for g in groups if g[0] > px + TOL[tk] * 0.3]
    below = [g for g in groups if g[0] < px - TOL[tk] * 0.3]
    rank = lambda g: (g[1], g[2], -abs(g[0] - px))
    techo = max(above, key=rank) if above else (round(px + (strad or 0)), 1, 1)
    piso = max(below, key=rank) if below else (round(px - (strad or 0)), 1, 1)
    half = (strad or 0) / 2
    pins = [g for g in groups if g[1] >= 2 and abs(g[0] - px) <= half]
    pin = max(pins, key=rank) if pins else None

    # PIN según estudio 2–3 nov (claude/flight_deck_pin_estudio.md):
    #   antes de 13:00 → "imán intradía" (nodo γ+ por volumen), no PIN de cierre
    #   13:00–14:00   → γ por volumen > 0: punto medio de majors; < 0: major −
    #   desde 14:00   → promedio de max pain por volumen, centro de |GEX vol| (±1.5σ) y strike más probable
    sig = (strad or 0) / 0.8
    stepk5 = 5 if tk == "SPX" else 1
    vsign = 1 if tv["gex"] > 0 else -1
    pmx, pmn = (mp, mn) if mr else (mpv or mp, mnv or mn)
    iman = mpv
    pinm, pinv, pinx = None, None, None
    if sig:
        ks0 = [k for k in m if abs(k - px) <= 3 * sig]
        def pay(s_):
            return sum(((m[k].get("c") or {}).get("totalVolume", 0) or 0) * max(0, s_ - k) +
                       ((m[k].get("p") or {}).get("totalVolume", 0) or 0) * max(0, k - s_) for k in ks0)
        grid = [round(px - 3 * sig + i * stepk5 / 5, 2) for i in range(int(6 * sig / (stepk5 / 5)) + 1)]
        maxpain = min(grid, key=pay) if ks0 else px
        cerca = {k: g for k, g in gkv.items() if abs(k - px) <= 1.5 * sig}
        w = sum(abs(g) for g in cerca.values())
        centro = sum(k * abs(g) for k, g in cerca.items()) / w if w else px
        moda = round(px / stepk5) * stepk5
        # los métodos se guardan cada minuto para el estudio (historial)
        rd = lambda x: round(float(x), 2)
        pinx = {"mid": rd((pmx + pmn) / 2) if pmx and pmn else None, "mneg": rd(pmn) if pmn else None,
                "mp": rd(maxpain), "cen": rd(centro), "near": moda, "vs": vsign}
    if mins >= 780 and pmn:
        # regla aprobada el 4-nov (auditoría 3 y 4-nov): desde 13:00 el PIN de cierre es el major − por volumen
        # (error 1.2–1.8 pts en 13–14 h); el punto medio de majors queda como alterno en el tablero
        pinv, pinm = pmn, "major − por volumen" + (f" · alterno punto medio {(pmx + pmn) / 2:.0f}" if pmx else "")
    if pinv is not None:
        pin = (round(pinv / stepk5) * stepk5 if tk == "SPX" else round(pinv, 1), 0, 0)
    pinz = [round(pin[0] - sig / 2, 1), round(pin[0] + sig / 2, 1)] if (pinv is not None and sig) else None
    # alerta "PIN en movimiento": major − o + se mueve ≥ 10 pts (SPX) o cambia el signo de γ por volumen en 15 min
    hk = "_pinh_" + tk
    hist = [h for h in state.get(hk, []) if mins - h[0] <= 15] + [[mins, pmx, pmn, vsign]]
    state[hk] = hist
    mt = 10 if tk == "SPX" else 1
    pinmov = None
    # con filtro: compara la mediana de los primeros 5 min contra la de los últimos 5 (el major oscila entre strikes vecinos minuto a minuto)
    if len(hist) >= 10:
        med = lambda xs: sorted(xs)[len(xs) // 2]
        a0, a1 = hist[:5], hist[-5:]
        mn0, mn1 = med([h[2] for h in a0]), med([h[2] for h in a1])
        mp0, mp1 = med([h[1] for h in a0]), med([h[1] for h in a1])
        s0, s1 = med([h[3] for h in a0]), med([h[3] for h in a1])
        if abs(mn1 - mn0) >= mt:
            pinmov = f"major − {mn0:g} → {mn1:g} en {mins - hist[0][0]} min"
        elif abs(mp1 - mp0) >= mt:
            pinmov = f"major + {mp0:g} → {mp1:g} en {mins - hist[0][0]} min"
        elif s0 != s1:
            pinmov = "la gamma por volumen cambió de signo"

    # bordes por delta (0DTE)
    d15 = [strike_delta(m, px, T0, 0.15, "p"), strike_delta(m, px, T0, 0.15, "c")]
    d10 = [strike_delta(m, px, T0, 0.10, "p"), strike_delta(m, px, T0, 0.10, "c")]

    def ic_at(ps, cs):
        w = WING[tk]
        try:
            return [ps, cs, round((mid(m[ps]["p"]) - mid(m[ps - w]["p"])) + (mid(m[cs]["c"]) - mid(m[cs + w]["c"])), 2), w]
        except (KeyError, TypeError):
            return [ps, cs, 0, w]
    stepk = 5 if tk == "SPX" else 1
    ic = ic2 = None
    if strad and ks:
        ic = ic_at(math.floor(rem[0] / stepk) * stepk, math.ceil(rem[1] / stepk) * stepk)
        dz = px - zg
        lo_m, hi_m = (SKEW[0], SKEW[1]) if dz < -GTOL[tk] else (SKEW[1], SKEW[0]) if dz > GTOL[tk] else (1.25, 1.25)
        ic2 = ic_at(math.floor((px - lo_m * strad) / stepk) * stepk, math.ceil((px + hi_m * strad) / stepk) * stepk)
    icd = ic_at(*d15) if all(d15) else None

    def vert(kind, a, b):
        """kind: bc (bear call vendido a<b), bp (bull put vendido a>b), cc (bull call comprado a<b), pp (bear put comprado a>b). Precio = mid."""
        try:
            if kind == "bc": v = mid(m[a]["c"]) - mid(m[b]["c"])
            elif kind == "bp": v = mid(m[a]["p"]) - mid(m[b]["p"])
            elif kind == "cc": v = mid(m[a]["c"]) - mid(m[b]["c"])
            else: v = mid(m[a]["p"]) - mid(m[b]["p"])
            return [a, b, round(v, 2)] if v == v else [a, b, 0]
        except (KeyError, TypeError):
            return [a, b, 0]
    w = WING[tk]
    snap = lambda x, up: (math.ceil(x / stepk) * stepk) if up else (math.floor(x / stepk) * stepk)
    atm_k = round(px / stepk) * stepk
    vs = {}
    if ks:
        cs_ = snap(max(techo[0], d15[1] or 0), True)
        vs["bc"] = vert("bc", cs_, cs_ + w)
        ps_ = snap(min(piso[0], d15[0] or 1e9), False)
        vs["bp"] = vert("bp", ps_, ps_ - w)
        tgt_up = snap(pmx if pmx > px + stepk else techo[0], True)
        vs["cc"] = vert("cc", atm_k, min(max(tgt_up, atm_k + w), atm_k + 3 * w))   # ancho entre 1 y 3 alas
        tgt_dn = snap(pmn if pmn < px - stepk else piso[0], False)
        vs["pp"] = vert("pp", atm_k, max(min(tgt_dn, atm_k - w), atm_k - 3 * w))

    edges = {"tol": TOL[tk] * 0.5,
             "up": [(em[1], "borde superior del rango"), (techo[0], "techo")],
             "dn": [(em[0], "borde inferior del rango"), (piso[0], "piso")]}
    fz = fuerza(closed, px, edges)

    r2 = lambda x: round(x, 2) if isinstance(x, float) else x
    rx = lambda d: {k: round(v, 2) for k, v in d.items() if k != "gross"}
    read = {"t": t_now.strftime("%-H:%M") if os.name != "nt" else t_now.strftime("%#H:%M"),
            "px": r2(px), "chg": fmt_pct(chg), "vix": vixinfo[tk], "macro": macro, "tf": tf, "tf2": tf2, "vw": r2(vw),
            "zg": r2(zg), "mp": r2(mp), "mn": r2(mn), "em": [em[0], em[1]], "strad": em[2] if len(em) > 2 else strad,
            "rem": rem, "bands": bands, "techo": [r2(techo[0]), techo[1], techo[2]],
            "pin": [r2(pin[0]), pin[1], pin[2]] if pin else None, "pinm": pinm, "pinz": pinz, "iman": iman, "pinmov": pinmov, "pinx": pinx, "piso": [r2(piso[0]), piso[1], piso[2]],
            "cw": [cw[0], cw[1]], "pw": [pw[0], pw[1]], "ic": ic, "ic2": ic2, "icd": icd, "d15": d15, "d10": d10,
            "fz": fz, "mom": mom, "momA": momA, "momAcc": [x[1] for x in mo], "vs": vs, "mpv": mpv, "mnv": mnv, "mvc": [mvc, round(near[mvc], 1)] if mvc is not None else None,
            "zge": zg_e, "flips": fl0, "flipsT": flT, "reg": reg,
            "gex": {"d0": rx(t0), "tot": rx(tT), "vol": rx(tv)},
            "auto": True, "gt": gt}
    return {"o": [b["open"] for b in todays], "h": [b["high"] for b in todays], "l": [b["low"] for b in todays],
            "c": [b["close"] for b in todays], "v": [b.get("volume", 0) for b in todays],
            "pre": [b["close"] for b in before][-200:], "rows": rows, "read": read}, prev


def vix_info(c, q):
    out = {}
    t = now()
    for sym, tks in (("$VIX", ["SPX", "SPY"]), ("$VXN", ["QQQ"])):
        x = q.get(sym, {}).get("quote", {})
        px = x.get("lastPrice")
        day15 = None
        try:
            b15 = resample(history(c, sym), 15)
            c15 = [b["close"] for b in b15]
            tr = trend(c15, px, 0.01) if px else 0
            tr2 = trend2(c15, px, None) if px else 0
            # velas de 15 min de hoy con su SMA 20, para la mini gráfica del tablero
            day15 = []
            for i, b in enumerate(b15):
                if dt.datetime.fromtimestamp(b["datetime"] / 1000, MX).date() == t.date():
                    w = c15[max(0, i - 19):i + 1]
                    day15.append([round(b["open"], 2), round(b["high"], 2), round(b["low"], 2), round(b["close"], 2),
                                  round(sum(w) / len(w), 2)])
        except Exception:
            tr = tr2 = 0
        hi = x.get("highPrice")
        nh = bool(px and hi and px >= hi - 0.02 and t.hour * 60 + t.minute >= 520)
        info = {"px": f"{px:.2f}" if px else "—", "chg": fmt_pct(x.get("netPercentChange") or 0), "tr": tr, "tr2": tr2,
                "hi": f"{hi:.2f}" if hi else None, "nh": nh, "c15": day15 or None}
        for t_ in tks:
            out[t_] = info
    return out


# Top 10 del S&P 500 por peso en el índice (SPY, 23-oct-2026; Alphabet = GOOGL + GOOG). Revisar cada trimestre.
TOP10 = [("NVDA", 7.77), ("AAPL", 7.72), ("MSFT", 5.93), ("GOOGL", 5.16), ("AMZN", 3.70),
         ("AVGO", 2.33), ("META", 2.16), ("TSLA", 1.60), ("LLY", 1.55), ("BRK/B", 1.54)]


def top10(q):
    """% de cada una desde la apertura de hoy (antes de abrir: vs cierre anterior), promedio ponderado y cuántas suben."""
    rows, sw, swp = [], 0.0, 0.0
    for sym, w in TOP10:
        x = (q.get(sym) or q.get(sym.replace("/", ".")) or {}).get("quote", {})
        last, op, pc = x.get("lastPrice"), x.get("openPrice"), x.get("closePrice")
        base = op if op else pc
        if not last or not base:
            continue
        pct = (last / base - 1) * 100
        rows.append([sym.replace("/", "."), round(pct, 2)])
        sw += w; swp += w * pct
    if not rows:
        return None
    wp = swp / sw
    up = sum(1 for r in rows if r[1] > 0)
    sem = 1 if (wp >= 0.15 and up >= 6) else -1 if (wp <= -0.15 and up <= 4) else 0
    return {"w": round(wp, 2), "up": up, "n": len(rows), "sem": sem, "s": rows}


def cycle(c, tok, state):
    t = now()
    today = t.date().isoformat()
    q = sget(c, "/quotes", symbols="$SPX,SPY,QQQ,$VIX,$VXN,$TNX", fields="quote")
    try:  # llamada aparte: si falla, el resto del ciclo sigue igual
        t10 = top10(sget(c, "/quotes", symbols=",".join(s_ for s_, _ in TOP10) + ",BRK.B", fields="quote"))
    except Exception as e:
        print("  top 10:", e)
        t10 = None
    tnx = q.get("$TNX", {}).get("quote", {})
    bps = (tnx.get("netChange") or 0) * 10
    macro = ("Alerta macro: " if abs(bps) >= 5 else "Sin alertas macro: ") + f"US10Y {bps:+.1f} pb · datos Schwab en tiempo real"
    manual = gh_manual(tok)
    vi = vix_info(c, q)
    out = {"date": today, "ts": t.strftime("%H:%M:%S"), "day": {}}
    prev = {}
    for tk in TICKERS:
        try:
            out[tk], prev[tk] = build_ticker(c, tk, q, manual, today, state, vi, macro)
            out[tk]["read"]["t10"] = t10
        except Exception as e:
            print(f"  {tk}: error {e}")
    d = t.date()
    out["day"] = {"title": f"Lectura del {DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}",
                  "prev": {k: round(v, 2) for k, v in prev.items() if v}}
    ok = gh_put(tok, json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    try:  # historial local por minuto (solo lecturas, sin velas ni llaves)
        HIST.mkdir(exist_ok=True)
        with open(HIST / f"{today}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": out["ts"], **{k: out[k]["read"] for k in TICKERS if k in out}}, ensure_ascii=False) + "\n")
    except Exception as e:
        print("  historial:", e)
    s = out.get("SPX", {}).get("read", {})
    print(f"{out['ts']}  SPX {s.get('px')}  rango {s.get('rem')}  techo {s.get('techo')}  piso {s.get('piso')}  "
          f"zgE {s.get('zge')} γ0 {s.get('gex',{}).get('d0',{}).get('gex')}  IC {s.get('ic2')}  tend {s.get('tf')}/{s.get('tf2')}  velas hoy {len(out.get('SPX', {}).get('c', []))} (última {state.get('_ultima_SPX')})  {'publicado' if ok else 'NO publicado'}")


def main():
    c = jload(SCHWAB)
    if not c.get("key"):
        sys.exit("Primero corre schwab_prueba.py para guardar las llaves de Schwab.")
    tok = gh_token()
    if "--subir" in sys.argv:  # python flightdeck_recolector.py --subir AAAA-MM-DD
        gh_put_hist(tok, sys.argv[sys.argv.index("--subir") + 1])
        return
    st, _ = gh_req(tok, "GET", f"/repos/{REPO}")
    if st != 200:
        sys.exit(f"El token de GitHub no tiene acceso al repo ({st}). Bórralo de {GH} y vuelve a crearlo.")
    state = jload(DIA)
    print("Recolector en marcha. Ctrl+C para detener.\n")
    while True:
        t0 = time.time()
        try:
            cycle(c, tok, state)
        except urllib.error.HTTPError as e:
            print(now().strftime("%H:%M:%S"), "Schwab", e.code, e.read()[:200])
        except Exception:
            traceback.print_exc()
        if "--una" in sys.argv:
            break
        t = now()
        if t.hour * 60 + t.minute >= 901:
            print("15:01 · mercado cerrado. Fin del día.")
            try:
                gh_put_hist(tok, t.date().isoformat())
            except Exception as e:
                print("historial a GitHub:", e)
            break
        time.sleep(max(5, 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
