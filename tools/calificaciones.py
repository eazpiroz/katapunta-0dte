"""Tarjeta de calificaciones de Katapunta: ¿qué tan bien leyó el sistema cada día, y cómo le fue al bot contra lo real?

Por día califica:
  · Régimen a las 10:00 ET (gamma + → rango/pin, gamma − → tendencia) contra la etiqueta del cierre.
  · Tu lectura de régimen (Bitácora) contra el cierre.
  · Clasificador del bot (reglas y Claude) a las 10:00, 11:00 y 13:00 ET.
  · PIN de las 15:00 ET contra el cierre oficial (error en puntos).
  · GEX propio contra la lectura manual del perfil γ (% de minutos con el mismo régimen).
  · Veto de Claude: dinero que habría ahorrado (vetos de perdedoras) y costado (vetos de ganadoras).
  · Bot de papel contra tu SPX 0DTE real (neto de comisiones, de bitacora_auto).

    python calificaciones.py              → escribe calificaciones.json
    python calificaciones.py --publicar   → además lo sube a la rama live (lo lee la página /calificaciones)
"""
import base64
import datetime as dt
import glob
import gzip
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
HIST = HERE / "historial"
GTOL = 3.0


def leer_jsonl(f):
    op = gzip.open if str(f).endswith(".gz") else open
    out = []
    with op(f, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("SPX"):
                out.append(j["SPX"])
    return out


def mins(t):
    h, m = t.split(":")[:2]
    return int(h) * 60 + int(m)


def fila(dia, t_cdmx):
    """Primera lectura en o después de t (hora CDMX, como la guarda el recolector)."""
    m = mins(t_cdmx)
    for r in dia:
        if mins(r["t"]) >= m:
            return r
    return None


def g_manual(r):
    if not r or not r.get("gt") or r.get("zg") is None or abs(r["zg"] - r["px"]) < 1e-6:
        return None
    return 1 if r["px"] > r["zg"] + GTOL else -1 if r["px"] < r["zg"] - GTOL else 0


def g_propio(r):
    reg = (r or {}).get("reg") or {}
    return 0 if reg.get("inc") or reg.get("d0") not in (1, -1) else reg["d0"]


def prediccion(g):
    return {1: "rango", -1: "tendencia"}.get(g, "incierto")


def acierta(pred, real):
    if not real or pred in (None, "incierto"):
        return None
    if pred == "rango":
        return real in ("rango", "pin")
    return pred == real


def json_url(url):
    try:
        return json.loads(urllib.request.urlopen(url, timeout=20).read())
    except Exception:
        return None


def real_0dte(fecha, bitacora):
    """SPX 0DTE real del día: bitacora_auto (neto de comisiones) si algún archivo cubre ese día; si no, la Bitácora.
    Junta todos los archivos porque la liquidación de un día llega en la lectura del día siguiente."""
    cubierto, vistos, tot = False, set(), 0.0
    for f in sorted((HERE / "bitacora_auto").glob("20??-??-??.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if (j.get("desde") or f.stem) <= fecha <= (j.get("hasta") or f.stem):
            cubierto = True
        for o in j.get("ops", []):
            k = (o.get("id"), o.get("fecha"), o.get("hora"))
            if o.get("motor") == "SPX 0DTE" and o.get("fecha") == fecha and k not in vistos:
                vistos.add(k); tot += o.get("neto") or 0
    if cubierto:
        return round(tot, 2), "schwab"
    xs = [o for o in (bitacora or {}).get("ops", []) if o.get("fecha") == fecha and o.get("motor") == "spx0dte"]
    if xs:
        return round(sum(o.get("pnl") or 0 for o in xs), 2), "bitacora"
    return None, None


def calificar():
    papel = json.loads((HERE / "papel.json").read_text(encoding="utf-8")) if (HERE / "papel.json").exists() else {}
    liq = json.loads((HERE / "liquidaciones.json").read_text(encoding="utf-8")) if (HERE / "liquidaciones.json").exists() else {}
    bitacora = json_url("https://0dte.katapunta.com/bitacora.json?t=%d" % dt.datetime.now().timestamp())
    archivos = {}
    for f in glob.glob(str(HIST / "20??-??-??.jsonl*")):
        archivos.setdefault(os.path.basename(f)[:10], f)      # .jsonl antes que .jsonl.gz si están los dos
    dias = []
    for fecha in sorted(archivos):
        dia = leer_jsonl(archivos[fecha])
        if len(dia) < 60:
            continue
        pd = (papel.get("dias") or {}).get(fecha, {})
        etiqueta = (pd.get("real") or {}).get("tipo")
        if not etiqueta:
            try:
                import papel_bot as P
                e = P.etiquetar_dia([(r["t"], r) for r in dia])
                etiqueta = e and e["tipo"]
            except Exception:
                etiqueta = None
        r10 = fila(dia, "09:00")
        gm, gp = g_manual(r10), g_propio(r10)
        g10 = gm if gm is not None else gp
        cierre = liq.get(fecha) or dia[-1]["px"]
        r15 = fila(dia, "14:00")
        pin = (r15 or {}).get("pin") or [None]
        pin = pin[0] if isinstance(pin, list) else pin
        # GEX propio contra manual, minuto a minuto
        par = [(g_manual(r), g_propio(r)) for r in dia]
        par = [(a, b) for a, b in par if a in (1, -1) and b in (1, -1)]
        coinc = round(100 * sum(a == b for a, b in par) / len(par)) if par else None
        # tu lectura (Bitácora)
        bd = ((bitacora or {}).get("dias") or {}).get(fecha) or {}
        tuya = bd.get("regimenLeido")
        # clasificador del bot
        clas = []
        for c in pd.get("clasif") or []:
            ia = c.get("ia") or {}
            clas.append({"hora": c.get("hora"), "reglas": (c.get("reglas") or {}).get("tipo"),
                         "ia": ia.get("tipo") if not ia.get("error") else None})
        # veto de Claude (modo registrar)
        ops = pd.get("operaciones") or []
        vet = [o for o in ops if (o.get("ia") or {}).get("decision") == "vetar"]
        ahorro = sum(-o["pnl"] for o in vet if o["pnl"] < 0)
        costo = sum(o["pnl"] for o in vet if o["pnl"] > 0)
        real, fuente = real_0dte(fecha, bitacora)
        dias.append({
            "fecha": fecha, "cierre": round(cierre, 2), "etiqueta": etiqueta,
            "regimen10": {"g": g10, "fuente": "manual" if gm is not None else "propio",
                          "pred": prediccion(g10), "ok": acierta(prediccion(g10), etiqueta)},
            "tuya": {"lectura": tuya, "ok": acierta(tuya, etiqueta) if tuya in ("rango", "tendencia", "pin") else None},
            "clasif": [dict(c, ok_reglas=acierta(c["reglas"], etiqueta) if c["reglas"] != "pin" else etiqueta == "pin",
                            ok_ia=(acierta(c["ia"], etiqueta) if c["ia"] != "pin" else etiqueta == "pin") if c["ia"] else None)
                       for c in clas],
            "pin15": {"pin": pin, "error": round(abs(pin - cierre), 1) if pin else None},
            "gex_coincide": coinc,
            "veto": {"n": len(vet), "ahorro": round(ahorro), "costo": round(costo)},
            "bot": {"pnl": pd.get("pnl"), "ops": pd.get("ops"), "ganadoras": pd.get("ganadoras")} if pd else None,
            "real": {"pnl": real, "fuente": fuente} if real is not None else None,
        })
    return {"actualizado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "dias": dias}


def publicar(data):
    import flightdeck_recolector as R
    tok = R.gh_token()
    path = f"/repos/{R.REPO}/contents/calificaciones.json"
    st, j = R.gh_req(tok, "GET", path + f"?ref={R.BRANCH}")
    body = {"message": "calificaciones", "branch": R.BRANCH,
            "content": base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()}
    if st == 200 and j.get("sha"):
        body["sha"] = j["sha"]
    st, j = R.gh_req(tok, "PUT", path, body)
    print("calificaciones → GitHub:", "ok" if st in (200, 201) else f"{st} {j.get('message')}")


def main():
    data = calificar()
    (HERE / "calificaciones.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for d in data["dias"]:
        r = d["regimen10"]
        print(f"{d['fecha']}  cierre {d['etiqueta'] or '—':9}  10:00 {r['pred']:9} {'✓' if r['ok'] else '✗' if r['ok'] is False else '·'}"
              f"  PIN err {d['pin15']['error'] if d['pin15']['error'] is not None else '—':>5}"
              f"  bot {d['bot']['pnl'] if d['bot'] else '—':>6}  real {d['real']['pnl'] if d['real'] else '—':>8}")
    if "--publicar" in sys.argv:
        publicar(data)


if __name__ == "__main__":
    main()
