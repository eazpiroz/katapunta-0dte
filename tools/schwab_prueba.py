#!/usr/bin/env python3
"""
Katapunta Flight Deck · prueba de conexión con la API de Schwab (solo Market Data).

Qué hace:
  1. La primera vez te pide Client ID y Client Secret (el secreto no se ve al escribirlo)
     y los guarda SOLO en tu computadora: %USERPROFILE%\\.katapunta\\schwab.json
  2. Abre tu navegador para que inicies sesión en Schwab y autorices la app.
  3. Lee cotizaciones de $SPX, SPY, QQQ y $VIX y la cadena 0DTE de SPX, e imprime un resumen.

No usa librerías externas: solo Python 3.9 o superior.
Uso:   python schwab_prueba.py
"""
import base64, datetime as dt, getpass, json, os, sys, time, urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path

CFG_DIR = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".katapunta"
CFG = CFG_DIR / "schwab.json"
REDIRECT = "https://127.0.0.1"
AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
API = "https://api.schwabapi.com/marketdata/v1"


def load():
    return json.loads(CFG.read_text()) if CFG.exists() else {}


def save(c):
    CFG_DIR.mkdir(exist_ok=True)
    CFG.write_text(json.dumps(c, indent=1))


def token_request(c, form):
    basic = base64.b64encode(f"{c['key']}:{c['secret']}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Authorization": "Basic " + basic,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            t = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"\nSchwab rechazó la solicitud ({e.code}). Respuesta: {body}")
        if "invalid_client" in body or "unauthorized_client" in body:
            print("→ Client ID o Client Secret no coinciden. Corre:  python schwab_prueba.py --reset")
            print("  y vuelve a copiarlos con el ojito (sin espacios). Si la app es recién creada, espera 15–30 min.")
        else:
            print("→ Lo más común: el código ya venció (dura ~30 s) o ya se usó. Corre el script otra vez y pega más rápido.")
        sys.exit(1)
    now = time.time()
    c["access"] = t["access_token"]
    c["access_exp"] = now + int(t.get("expires_in", 1800)) - 60
    if "refresh_token" in t:
        c["refresh"] = t["refresh_token"]
        if form.get("grant_type") == "authorization_code":
            c["refresh_exp"] = now + 7 * 24 * 3600 - 3600
    save(c)


def login(c):
    url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": c["key"], "redirect_uri": REDIRECT})
    print("\nSe va a abrir Schwab en tu navegador. Inicia sesión y autoriza la app.")
    print("Al final el navegador mostrará un error de 'no se puede acceder a 127.0.0.1': es normal.")
    print("Copia la dirección COMPLETA de la barra (empieza con https://127.0.0.1/?code=...) y pégala aquí.")
    print("El código vence en ~30 segundos: ten esta ventana lista para pegar.\n")
    input("Presiona Enter para abrir Schwab... ")
    webbrowser.open(url)
    back = input("Dirección: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    if "code" not in q:
        sys.exit("No encontré 'code' en esa dirección. Corre el script otra vez.")
    token_request(c, {"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT})
    print("Sesión autorizada. El permiso dura 7 días.\n")


def access(c):
    if c.get("access") and time.time() < c.get("access_exp", 0):
        return c["access"]
    if c.get("refresh") and time.time() < c.get("refresh_exp", 0):
        try:
            token_request(c, {"grant_type": "refresh_token", "refresh_token": c["refresh"]})
            return c["access"]
        except Exception as e:
            print("No se pudo renovar el permiso:", e)
    login(c)
    return c["access"]


def get(c, path, **params):
    url = API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access(c), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main():
    c = load()
    if "--reset" in sys.argv:
        c = {}
    if not c.get("key") or not c.get("secret"):
        print("Primera vez: copia los datos de tu app en developer.schwab.com (ojito junto a cada campo).")
        c["key"] = input("Client ID: ").strip()
        c["secret"] = getpass.getpass("Client Secret (no se verá al escribir): ").strip()
        save(c)
        print("Guardado en", CFG)

    q = get(c, "/quotes", symbols="$SPX,SPY,QQQ,$VIX", fields="quote")
    print("COTIZACIONES")
    for s in ["$SPX", "SPY", "QQQ", "$VIX"]:
        x = q.get(s, {}).get("quote", {})
        t = x.get("quoteTime") or x.get("tradeTime")
        hora = dt.datetime.fromtimestamp(t / 1000).strftime("%H:%M:%S") if t else "?"
        print(f"  {s:5} {x.get('lastPrice')}  ({x.get('netPercentChange', 0):+.2f}%)  hora {hora}")

    hoy = dt.date.today().isoformat()
    ch = get(c, "/chains", symbol="$SPX", contractType="ALL", strikeCount=40,
             includeUnderlyingQuote="true", fromDate=hoy, toDate=hoy)
    S = ch.get("underlyingPrice")
    calls, puts = ch.get("callExpDateMap", {}), ch.get("putExpDateMap", {})
    print(f"\nCADENA SPX de hoy ({hoy}) · subyacente {S}")
    print("  vencimientos:", list(calls.keys()))
    for exp in calls:
        ks = sorted(calls[exp].keys(), key=float)
        atm = min(ks, key=lambda k: abs(float(k) - S))
        cc, pp = calls[exp][atm][0], puts.get(exp, {}).get(atm, [{}])[0]
        mid = lambda o: (o.get("bid", 0) + o.get("ask", 0)) / 2
        print(f"  {exp}: {len(ks)} strikes · ATM {atm} · straddle {mid(cc) + mid(pp):.2f}")
        print(f"    call {atm}: bid {cc.get('bid')} ask {cc.get('ask')} vol {cc.get('totalVolume')} OI {cc.get('openInterest')} gamma {cc.get('gamma')}")
        print(f"    put  {atm}: bid {pp.get('bid')} ask {pp.get('ask')} vol {pp.get('totalVolume')} OI {pp.get('openInterest')} gamma {pp.get('gamma')}")
    print("\nPrueba terminada. Copia todo este texto y mándamelo (no incluye tus llaves).")


if __name__ == "__main__":
    main()
