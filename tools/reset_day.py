#!/usr/bin/env python3
"""Deja data.json listo para un día nuevo (on=false, sin lecturas).
Uso: python3 tools/reset_day.py AAAA-MM-DD "Lectura del martes 3 de noviembre" '{"SPX":7541.61,"SPY":752.15,"QQQ":681.04}' '[[540,"9:00 · Evento"]]'
"""
import json, pathlib, sys
e = lambda: {"o": [], "h": [], "l": [], "c": [], "v": [], "reads": [], "rows": []}
p = pathlib.Path(__file__).resolve().parent.parent / 'data.json'
old = json.loads(p.read_text()) if p.exists() else {}
d = {"on": False, "SPX": e(), "SPY": e(), "QQQ": e()}
if old.get('build'): d['build'] = old['build']
if len(sys.argv) > 1: d['date'] = sys.argv[1]
day = {}
if len(sys.argv) > 2: day['title'] = sys.argv[2]
if len(sys.argv) > 3: day['prev'] = json.loads(sys.argv[3])
if len(sys.argv) > 4: day['agenda'] = json.loads(sys.argv[4])
if day: d['day'] = day
p.write_text(json.dumps(d, separators=(',', ':'), ensure_ascii=False))
print('data.json listo para', d.get('date'))
