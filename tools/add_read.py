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
