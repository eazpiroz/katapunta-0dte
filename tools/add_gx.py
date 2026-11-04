#!/usr/bin/env python3
"""Guarda en data.json la serie del perfil γ (Gexbot Classic, volumen) en velas de 5 min.
Uso: python3 tools/add_gx.py SPX serie.json [archivo.json]
serie.json = [[i, zero_gamma, major_pos_vol, major_neg_vol, net_gex_vol_B|null], ...]
  i = índice de la vela de 5 min desde las 8:30 CDMX (0 = 8:30–8:35).
Reemplaza la serie del ticker (cada lectura trae la historia completa desde la apertura).
Si se da un tercer argumento, escribe en ese archivo (p. ej. archivo/AAAA-MM-DD.json) en lugar de data.json.
"""
import json, sys, pathlib

root = pathlib.Path(__file__).resolve().parent.parent
tk, src = sys.argv[1], sys.argv[2]
dst = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else root / 'data.json'
d = json.loads(dst.read_text())
serie = json.loads(pathlib.Path(src).read_text())
serie = sorted(([int(r[0])] + [round(float(x), 2) for x in r[1:4]] + [r[4] if len(r) > 4 else None]) for r in serie)
d.setdefault('day', {}).setdefault('gx', {})[tk] = serie
dst.write_text(json.dumps(d, separators=(',', ':')))
print(tk, len(serie), 'velas →', dst.name)
