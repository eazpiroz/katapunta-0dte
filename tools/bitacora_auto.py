"""Bitácora automática: lee de Schwab las operaciones reales de la cuenta (SOLO LECTURA) y las clasifica por motor.

No existe en este archivo ninguna llamada para mandar, cambiar o cancelar órdenes: solo GET a
/accounts/accountNumbers, /accounts (posiciones) y /accounts/{hash}/transactions.
No guarda números de cuenta completos ni el hash de la cuenta.

    python bitacora_auto.py --probar 2026-11-16 2026-11-20   → imprime y guarda bitacora_auto/muestra.json
    python bitacora_auto.py                                   → hoy
    python bitacora_auto.py 2026-11-18                        → un día

Motores (regla confirmada por Eduardo, 21-nov-2026):
  SPX/SPXW que vence el mismo día           → SPX 0DTE
  abre y cierra el mismo día (no SPX 0DTE)  → Scalping
  opción con > 365 días al abrir            → LEAPS
  put vendido en acción, call cubierto, o acción/ETF con ≥ 100 acciones en cartera → Wheel
  todo lo demás                             → Swing
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flightdeck_recolector as R  # noqa: E402  (llaves y token del recolector, en ~/.katapunta)

TRADER = "https://api.schwabapi.com/trader/v1"
OUT = Path(__file__).resolve().parent / "bitacora_auto"
ET = dt.timezone(dt.timedelta(hours=-5))   # noviembre–marzo; el horario se reporta en ET


def tget(c, path, **params):
    """Solo GET a la API de cuentas."""
    assert path.startswith("/accounts"), "bitacora_auto solo lee cuentas"
    url = TRADER + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + R.access(c), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def cuenta(c):
    nums = tget(c, "/accounts/accountNumbers")
    if not nums:
        sys.exit("Schwab no devolvió cuentas.")
    return nums[0]["hashValue"], nums[0]["accountNumber"][-4:]


def acciones_en_cartera(c):
    """{símbolo: cantidad} de acciones y ETF (para la regla de Wheel ≥ 100)."""
    out = {}
    for a in tget(c, "/accounts", fields="positions"):
        for p in a.get("securitiesAccount", {}).get("positions", []):
            ins = p.get("instrument", {})
            if ins.get("assetType") in ("EQUITY", "COLLECTIVE_INVESTMENT", "ETF"):
                out[ins.get("symbol")] = (p.get("longQuantity") or 0) - (p.get("shortQuantity") or 0)
    return out


def transacciones(c, h, d1, d2):
    ini = f"{d1}T00:00:00.000Z"
    fin = (dt.date.fromisoformat(d2) + dt.timedelta(days=1)).isoformat() + "T06:00:00.000Z"
    return tget(c, f"/accounts/{h}/transactions", startDate=ini, endDate=fin, types="TRADE")


def patas(t):
    """Patas de una transacción (sin la línea de comisiones / efectivo)."""
    out = []
    for it in t.get("transferItems", []):
        ins = it.get("instrument", {})
        tipo = ins.get("assetType")
        if tipo in (None, "CURRENCY") or it.get("feeType"):
            continue
        out.append({
            "tipo": tipo, "simbolo": ins.get("symbol"), "subyacente": ins.get("underlyingSymbol") or ins.get("symbol"),
            "pc": ins.get("putCall"), "strike": ins.get("strikePrice"),
            "vence": (ins.get("expirationDate") or "")[:10] or None,
            "cant": it.get("amount"), "precio": it.get("price"), "efecto": it.get("positionEffect"),
        })
    return out


def hora_et(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00")).astimezone(ET)
    except Exception:
        return None


def motor(op, cartera, mismo_dia_cerrada):
    p0 = op["patas"][0] if op["patas"] else {}
    sub = (p0.get("subyacente") or "").replace("$", "")
    fecha = op["fecha"]
    if sub in ("SPX", "SPXW") and all(p.get("vence") == fecha for p in op["patas"]):
        return "SPX 0DTE"
    if op.get("liquidacion"):
        return "SPX 0DTE"
    if mismo_dia_cerrada:
        return "Scalping"
    opc = [p for p in op["patas"] if p["tipo"] == "OPTION"]
    if opc and p0.get("efecto") == "OPENING":
        dias = (dt.date.fromisoformat(opc[0]["vence"]) - dt.date.fromisoformat(fecha)).days if opc[0]["vence"] else 0
        if dias > 365 and all((p["cant"] or 0) > 0 for p in opc):
            return "LEAPS"
    if len(opc) == 1 and (opc[0]["cant"] or 0) < 0:          # una sola pata vendida: put vendido o call cubierto
        if opc[0]["pc"] == "PUT" or cartera.get(sub, 0) >= 100:
            return "Wheel"
    if not opc and cartera.get(sub, 0) >= 100:
        return "Wheel"
    if (len(opc) == 1 and opc[0]["pc"] == "CALL" and cartera.get(sub, 0) >= 100
            and opc[0]["efecto"] == "CLOSING" and (opc[0]["cant"] or 0) > 0):
        return "Wheel"                                         # recompra de un call cubierto
    return "Swing"


def leer(d1, d2):
    c = R.jload(R.SCHWAB)
    h, ult4 = cuenta(c)
    cartera = acciones_en_cartera(c)
    tx = transacciones(c, h, d1, d2)
    ops = []
    for t in tx:
        ts = hora_et(t.get("time") or t.get("tradeDate") or "")
        ps = patas(t)
        if not ps or not ts:
            continue
        fecha = ts.date().isoformat()
        liq = False
        # liquidación en efectivo de SPX/SPXW vencido (Schwab la registra de madrugada del día siguiente)
        if all(p["tipo"] == "OPTION" and (p["subyacente"] or "").replace("$", "") in ("SPX", "SPXW")
               and p["vence"] and p["vence"] < fecha and p["efecto"] == "CLOSING" for p in ps):
            fecha, liq = ps[0]["vence"], True
            t = dict(t, orderId=f"liq-{fecha}")
        ops.append({"id": str(t.get("orderId") or t.get("activityId")), "fecha": fecha, "liquidacion": liq,
                    "hora": "16:00" if liq else ts.strftime("%H:%M"), "neto": t.get("netAmount"), "patas": ps})
    # agrupar patas de la misma orden (Schwab manda una transacción por pata en algunos spreads)
    por_orden = {}
    for o in ops:
        k = (o["id"], o["fecha"])
        if k in por_orden:
            por_orden[k]["patas"] += o["patas"]; por_orden[k]["neto"] = (por_orden[k]["neto"] or 0) + (o["neto"] or 0)
        else:
            por_orden[k] = o
    ops = sorted(por_orden.values(), key=lambda o: (o["fecha"], o["hora"]))
    # ¿el mismo contrato se abrió y se cerró el mismo día? (cuenta para la orden que abre y para la que cierra)
    efectos = {}
    for o in ops:
        for p in o["patas"]:
            efectos.setdefault((o["fecha"], p["simbolo"]), set()).add(p["efecto"])
    for o in ops:
        dia = any({"OPENING", "CLOSING"} <= efectos.get((o["fecha"], p["simbolo"]), set()) for p in o["patas"])
        o["motor"] = motor(o, cartera, dia)
    return ult4, cartera, ops


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    hoy = dt.datetime.now(ET).date().isoformat()
    d1 = args[0] if args else hoy
    d2 = args[1] if len(args) > 1 else d1
    try:
        ult4, cartera, ops = leer(d1, d2)
    except urllib.error.HTTPError as e:
        sys.exit(f"Schwab respondió {e.code}: {e.read().decode(errors='replace')[:300]}")
    print(f"Cuenta …{ult4} · {d1} a {d2} · {len(ops)} órdenes\n")
    tot = {}
    for o in ops:
        desc = " / ".join(f"{p['cant']:+g} {p['subyacente']} {p['vence'] or ''} {p['strike'] or ''}{(p['pc'] or '')[:1]} {(p['efecto'] or '')[:4]}".replace("  ", " ")
                          for p in o["patas"])
        print(f"{o['fecha']} {o['hora']} ET  {o['motor']:9} neto {o['neto']:+10,.2f}  {desc}")
        tot[o["motor"]] = tot.get(o["motor"], 0) + (o["neto"] or 0)
    print("\nFlujo neto por motor (efectivo, no P&L):", {k: round(v, 2) for k, v in tot.items()})
    OUT.mkdir(exist_ok=True)
    nombre = "muestra.json" if "--probar" in sys.argv else f"{d1}.json"
    (OUT / nombre).write_text(json.dumps({"desde": d1, "hasta": d2, "ops": ops}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nGuardado en bitacora_auto/{nombre}")


if __name__ == "__main__":
    main()
