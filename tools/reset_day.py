#!/usr/bin/env python3
"""Deja data.json vacío para un día nuevo (on=false)."""
import json, pathlib
e = lambda: {"o": [], "h": [], "l": [], "c": [], "v": [], "reads": [], "rows": []}
p = pathlib.Path(__file__).resolve().parent.parent / 'data.json'
p.write_text(json.dumps({"on": False, "SPX": e(), "SPY": e(), "QQQ": e()}))
