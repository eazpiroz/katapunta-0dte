"""Prueba de solo lectura: ¿qué cuentas y posiciones ve el permiso de Schwab? (pendiente #15)

Usa las llaves y el token que ya guarda flightdeck_recolector.py. Solo hace GET a
/trader/v1/accounts: no existe en este archivo ninguna llamada para mandar órdenes.
Muestra solo los últimos 4 dígitos de cada cuenta.

    python schwab_cuentas.py
"""
import json, os, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flightdeck_recolector as R

TRADER = "https://api.schwabapi.com/trader/v1"


def tget(c, path):
    req = urllib.request.Request(TRADER + path, headers={"Authorization": "Bearer " + R.access(c), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def main():
    c = R.jload(R.SCHWAB)
    try:
        nums = tget(c, "/accounts/accountNumbers")
    except Exception as e:
        body = getattr(e, "read", lambda: b"")().decode(errors="replace")[:300]
        sys.exit(f"Schwab no dejó leer cuentas: {e} {body}")
    print(f"El permiso ve {len(nums)} cuenta(s): " + ", ".join("…" + n["accountNumber"][-4:] for n in nums))
    accts = tget(c, "/accounts?fields=positions")
    for a in accts:
        s = a.get("securitiesAccount", {})
        b = s.get("currentBalances", {})
        pos = s.get("positions", [])
        print(f"\nCuenta …{str(s.get('accountNumber', ''))[-4:]} · {s.get('type')} · valor de liquidación ${b.get('liquidationValue', 0):,.2f} · {len(pos)} posiciones")
        for p in sorted(pos, key=lambda p: p.get("instrument", {}).get("assetType", "")):
            ins = p.get("instrument", {})
            q = (p.get("longQuantity") or 0) - (p.get("shortQuantity") or 0)
            print(f"  {ins.get('assetType', ''):8} {ins.get('symbol', ''):26} {q:+g}  P/L día ${p.get('currentDayProfitLoss', 0):,.2f}")


if __name__ == "__main__":
    main()
