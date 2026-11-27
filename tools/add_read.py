#!/usr/bin/env python3
"""Agrega una lectura a data.json.
Uso: python3 tools/add_read.py lectura.json
lectura.json = {"SPX": {"o":[..],"h":[..],"l":[..],"c":[..],"v":[..],"read":{...},"rows":[[k,cOI,pOI,cV,pV,gex],...]}, "SPY": {...}, "QQQ": {...}}
Velas: solo las nuevas ya cerradas. Pone on=true.
"""
import json, sys, pathlib
root = pathlib.Path(__file__).resolve().parent.parent
d = json.loads((root / 'data.json').read_text())
new = json.loads(pathlib.Path(sys.argv[1]).read_text())

# Guarda contra el bug del 27-nov-2026: Gexbot Classic a veces queda en modo
# replay/sin datos y su panel "volume" devuelve zg=mp=mn=nv=0 para los tres
# tickers a la vez. Eso es estadísticamente imposible con una lectura real de
# mercado, así que si CUALQUIER ticker trae zg=mp=mn=0 en la lectura, se
# rechaza TODO el archivo antes de tocar data.json (nada de publicaciones a
# medias). El runner del cada-hora debe tratar esto como fallo y abortar.
for tk, x in new.items():
    r = x.get('read')
    if r and r.get('zg', None) == 0 and r.get('mp', None) == 0 and r.get('mn', None) == 0:
        sys.exit(f"RECHAZADO: {tk}.read trae zg=mp=mn=0 (lectura de Gexbot inválida/en replay, no es un dato real) — no se escribió nada en data.json")

d['on'] = True
import datetime
d.setdefault('date', datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-6))).date().isoformat())
for tk, x in new.items():
    L = d[tk]
    for k in 'ohlcv':
        L[k] += x.get(k, [])
    if x.get('read'): L['reads'].append(x['read'])
    if x.get('rows'): L['rows'].append(x['rows'])
    assert len({len(L[k]) for k in 'ohlcv'}) == 1, tk + ': velas desalineadas'
(root / 'data.json').write_text(json.dumps(d, separators=(',', ':')))
print({tk: (len(d[tk]['c']), len(d[tk]['reads'])) for tk in ('SPX', 'SPY', 'QQQ')})
