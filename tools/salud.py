"""Alarma de salud de Katapunta: avisa al celular (solo a tu tema de ntfy) si algo se cae.

Lo corre el Programador de tareas cada 10 min, lunes a viernes. Revisa:
  1. Sesión de Schwab: avisa cuando faltan menos de 30 h para que venza (hay que hacer --login).
  2. Recolector: en sesión (9:31–16:00 ET), si el historial de hoy lleva más de 5 min sin escribir.
  3. Bot de papel: en sesión, si no está corriendo (su candado local está libre).
  4. Pre-market: desde las 9:20 ET, si el reporte de hoy no se publicó.

Cada problema avisa como máximo una vez por hora. No lee ni manda órdenes; no imprime llaves.

    python salud.py            → revisa y avisa
    python salud.py --probar   → solo imprime lo que ve, sin mandar avisos
"""
import datetime as dt
import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ESTADO = HERE / "salud_estado.json"
HOLI = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
EARLY = {"2026-11-27", "2026-12-24", "2027-11-26"}


def ahora_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=-5)))


def revisar():
    t = ahora_et()
    hoy, m = t.date().isoformat(), t.hour * 60 + t.minute
    habil = t.weekday() < 5 and hoy not in HOLI
    cierre = 13 * 60 if hoy in EARLY else 16 * 60
    sesion = habil and 9 * 60 + 31 <= m < cierre
    problemas = []

    # 1 · sesión de Schwab
    try:
        import flightdeck_recolector as R
        c = R.jload(R.SCHWAB)
        horas = (c.get("refresh_exp", 0) - time.time()) / 3600
        if horas < 30:
            cuando = "ya venció" if horas <= 0 else f"vence en {horas:.0f} h"
            problemas.append(("schwab", f"La sesión de Schwab {cuando}. En la carpeta API: python schwab_prueba.py --login"))
    except Exception as e:
        problemas.append(("schwab", f"No pude revisar la sesión de Schwab: {type(e).__name__}"))

    if sesion:
        # 2 · recolector
        f = HERE / "historial" / f"{hoy}.jsonl"
        edad = (time.time() - f.stat().st_mtime) / 60 if f.exists() else None
        if edad is None or edad > 5:
            problemas.append(("recolector", "El recolector no ha escrito datos en "
                              + ("todo el día" if edad is None else f"{edad:.0f} min")
                              + ". Revisa su ventana (puede estar pidiendo el login de Schwab)."))
        # 3 · bot de papel (su candado es un puerto local ocupado mientras corre)
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 47321))
            problemas.append(("bot", "El bot de papel no está corriendo. Ábrelo con iniciar_papel.bat."))
        except OSError:
            pass
        finally:
            s.close()

    # 4 · pre-market
    if habil and 9 * 60 + 20 <= m < cierre:
        try:
            j = json.loads(urllib.request.urlopen("https://premarket.katapunta.com/premarket.json?t=%d" % time.time(), timeout=15).read())
            if j.get("fecha") != hoy:
                problemas.append(("premarket", f"El reporte Pre-market de hoy no se ha publicado (el último es del {j.get('fecha')})."))
        except Exception as e:
            problemas.append(("premarket", f"No pude leer el Pre-market: {type(e).__name__}"))
    return t, problemas


def main():
    t, problemas = revisar()
    if "--probar" in sys.argv:
        print(f"{t:%Y-%m-%d %H:%M} ET · " + ("todo bien" if not problemas else f"{len(problemas)} aviso(s)"))
        for k, txt in problemas:
            print(f"  [{k}] {txt}")
        return
    try:
        est = json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        est = {}
    nuevos = [(k, txt) for k, txt in problemas if time.time() - est.get(k, 0) > 3600]
    if not nuevos:
        return
    import papel_bot as P
    for k, txt in nuevos:
        P.mandar_yo(f"KATAPUNTA · Revisar {k}", txt, ["warning"], 5)
        est[k] = time.time()
    ESTADO.write_text(json.dumps(est), encoding="utf-8")


if __name__ == "__main__":
    main()
