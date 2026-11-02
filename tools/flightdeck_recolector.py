#!/usr/bin/env python3
"""
Katapunta Flight Deck · recolector automático (Schwab Market Data → 0dte.katapunta.com)

Cada minuto, de la hora en que lo abras hasta las 15:01 CDMX:
  - lee de Schwab cotizaciones, velas de 5 min y la cadena 0DTE de SPX, SPY y QQQ
  - calcula straddle, rango restante, GEX por strike, muros, techo/piso/PIN e iron condor de referencia
  - toma los majors y el zero γ de la última lectura manual (data.json), si existe
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


def history(c, sym):
    end = int(time.time() * 1000)
    j = sget(c, "/pricehistory", symbol=sym, periodType="day", frequencyType="minute", frequency=5,
             startDate=end - 14 * 24 * 3600 * 1000, endDate=end, needExtendedHoursData="false")
    return j.get("candles", [])


def chain(c, sym, S, today, tk):
    j = sget(c, "/chains", symbol=sym, contractType="ALL", strikeCount=80 if tk == "SPX" else 40,
             includeUnderlyingQuote="true", fromDate=today, toDate=today)
    m = {}
    for side, key in (("c", "callExpDateMap"), ("p", "putExpDateMap")):
        for exp, ks in j.get(key, {}).items():
            for k, lst in ks.items():
                o = lst[0]
                if tk == "SPX" and not str(o.get("symbol", "")).startswith("SPXW"):
                    continue
                m.setdefault(float(k), {})[side] = o
    return m


def mid(o):
    if not o:
        return float("nan")
    return (o.get("bid", 0) + o.get("ask", 0)) / 2


def clusters(cands, tol):
    """cands: [(nivel, fuente)] → grupos [(centro, n_fuentes_distintas)]."""
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
        out.append((center, len(g["srcs"])))
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

    # velas
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
    tech = {"5m SMA200": sma(c5, 200), "15m SMA100": sma(c15, 100), "15m SMA200": sma(c15, 200),
            "15m EMA100": ema(c15, 100), "15m EMA200": ema(c15, 200)}

    # cadena
    m = chain(c, sym, px, today, tk)
    ks = sorted(k for k in m if "c" in m[k] and "p" in m[k])
    atm = min(ks, key=lambda k: abs(k - px)) if ks else None
    strad = round(mid(m[atm]["c"]) + mid(m[atm]["p"]), 2) if atm else None
    em_key = "em_" + tk
    if t_now.hour * 60 + t_now.minute >= 510 and state.get(em_key + "_d") != today and strad:
        state[em_key] = [round(px - strad, 2), round(px + strad, 2), strad]
        state[em_key + "_d"] = today
        jsave(DIA, state)
    em = state.get(em_key) if state.get(em_key + "_d") == today else [round(px - strad, 2), round(px + strad, 2), strad]
    rem = [round(px - strad, 1), round(px + strad, 1)] if strad else None

    rows, cw, pw, gx, cv, pv = [], (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)
    gexk = {}
    for k in sorted(m):
        if abs(k - px) > SPAN[tk]:
            continue
        cc, pp = m[k].get("c", {}), m[k].get("p", {})
        cOI, pOI = cc.get("openInterest", 0) or 0, pp.get("openInterest", 0) or 0
        cV, pV = cc.get("totalVolume", 0) or 0, pp.get("totalVolume", 0) or 0
        g = ((cc.get("gamma") or 0) * cOI - (pp.get("gamma") or 0) * pOI) * 100 * px * px * 0.01 / 1e6
        gexk[k] = g
        if k > px and cOI > cw[1]: cw = (k, cOI)
        if k < px and pOI > pw[1]: pw = (k, pOI)
        if abs(g) > abs(gx[1]): gx = (k, g)
        if cV > cv[1]: cv = (k, cV)
        if pV > pv[1]: pv = (k, pV)
        if round(k) % STEP[tk] == 0 and float(k).is_integer():
            rows.append([int(k) if tk == "SPX" else k, cOI, pOI, cV, pV, round(g, 1)])

    # majors y zero γ: de la lectura manual si es de hoy; si no, del GEX propio
    mr = None
    if manual.get("date") == today and manual.get(tk, {}).get("reads"):
        mr = manual[tk]["reads"][-1]
    if mr:
        zg, mp, mn, gt = mr["zg"], mr["mp"], mr["mn"], mr["t"]
    else:
        pos = {k: g for k, g in gexk.items() if g > 0}
        neg = {k: g for k, g in gexk.items() if g < 0}
        mp = max(pos, key=pos.get) if pos else px
        mn = min(neg, key=neg.get) if neg else px
        zg, cum, prevk = px, 0.0, None
        for k in sorted(gexk):
            nc = cum + gexk[k]
            if prevk is not None and cum < 0 <= nc:
                zg = k
            cum, prevk = nc, k
        gt = None

    cands = [(mp, "major +"), (mn, "major −"), (cw[0] or None, "muro call"), (pw[0] or None, "muro put"),
             (gx[0] or None, "máx GEX"), (cv[0] or None, "vol calls"), (pv[0] or None, "vol puts")]
    cands += [(v, n) for n, v in tech.items() if v]
    groups = clusters(cands, TOL[tk])
    above = [g for g in groups if g[0] > px + TOL[tk] * 0.3]
    below = [g for g in groups if g[0] < px - TOL[tk] * 0.3]
    techo = max(above, key=lambda g: (g[1], -abs(g[0] - px))) if above else (round(px + (strad or 0)), 1)
    piso = max(below, key=lambda g: (g[1], -abs(g[0] - px))) if below else (round(px - (strad or 0)), 1)
    half = (strad or 0) / 2
    pins = [g for g in groups if g[1] >= 3 and abs(g[0] - px) <= half]
    pin = max(pins, key=lambda g: g[1]) if pins else None

    ic = None
    if rem and ks:
        w = WING[tk]
        stepk = 5 if tk == "SPX" else 1
        ps = math.floor(rem[0] / stepk) * stepk
        cs = math.ceil(rem[1] / stepk) * stepk
        try:
            cr = (mid(m[ps]["p"]) - mid(m[ps - w]["p"])) + (mid(m[cs]["c"]) - mid(m[cs + w]["c"]))
            ic = [ps, cs, round(cr, 2), w]
        except KeyError:
            ic = [ps, cs, 0, w]

    r2 = lambda x: round(x, 2) if isinstance(x, float) else x
    read = {"t": t_now.strftime("%-H:%M") if os.name != "nt" else t_now.strftime("%#H:%M"),
            "px": r2(px), "chg": fmt_pct(chg), "vix": vixinfo[tk], "macro": macro, "tf": tf,
            "zg": r2(zg), "mp": r2(mp), "mn": r2(mn), "em": [em[0], em[1]], "strad": em[2] if len(em) > 2 else strad,
            "rem": rem, "techo": [r2(techo[0]), techo[1]], "pin": [r2(pin[0]), pin[1]] if pin else None,
            "piso": [r2(piso[0]), piso[1]], "cw": [cw[0], cw[1]], "pw": [pw[0], pw[1]], "ic": ic,
            "auto": True, "gt": gt}
    return {"o": [b["open"] for b in todays], "h": [b["high"] for b in todays], "l": [b["low"] for b in todays],
            "c": [b["close"] for b in todays], "v": [b.get("volume", 0) for b in todays],
            "pre": [b["close"] for b in before][-200:], "rows": rows, "read": read}, prev


def vix_info(c, q):
    out = {}
    for sym, tks in (("$VIX", ["SPX", "SPY"]), ("$VXN", ["QQQ"])):
        x = q.get(sym, {}).get("quote", {})
        px = x.get("lastPrice")
        try:
            c15 = [b["close"] for b in resample(history(c, sym), 15)]
            tr = trend(c15, px, 0.01) if px else 0
        except Exception:
            tr = 0
        info = {"px": f"{px:.2f}" if px else "—", "chg": fmt_pct(x.get("netPercentChange") or 0), "tr": tr}
        for t in tks:
            out[t] = info
    return out


def cycle(c, tok, state):
    t = now()
    today = t.date().isoformat()
    q = sget(c, "/quotes", symbols="$SPX,SPY,QQQ,$VIX,$VXN,$TNX", fields="quote")
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
        except Exception as e:
            print(f"  {tk}: error {e}")
    d = t.date()
    out["day"] = {"title": f"Lectura del {DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}",
                  "prev": {k: round(v, 2) for k, v in prev.items() if v}}
    ok = gh_put(tok, json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    s = out.get("SPX", {}).get("read", {})
    print(f"{out['ts']}  SPX {s.get('px')}  rango {s.get('rem')}  techo {s.get('techo')}  piso {s.get('piso')}  "
          f"IC {s.get('ic')}  velas hoy {len(out.get('SPX', {}).get('c', []))} (última {state.get('_ultima_SPX')})  {'publicado' if ok else 'NO publicado'}")


def main():
    c = jload(SCHWAB)
    if not c.get("key"):
        sys.exit("Primero corre schwab_prueba.py para guardar las llaves de Schwab.")
    tok = gh_token()
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
            break
        time.sleep(max(5, 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
