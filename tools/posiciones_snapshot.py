"""Arma posiciones.json (foto horaria pública del tablero de Posiciones).
Uso: python3 tools/posiciones_snapshot.py DIR_DOCS QUOTES.json
DIR_DOCS: carpeta con un JSON por documento de la colección "posiciones" (nombre = id).
QUOTES.json: respuesta de TradingView get-symbol-data-batch (payload con data{SYM:{close,change}}).
Escribe posiciones.json en la raíz del repositorio."""
import json, sys, glob, os, datetime
docs_dir, qfile = sys.argv[1], sys.argv[2]
rows = []
for f in sorted(glob.glob(os.path.join(docs_dir, "*.json"))):
    d = json.load(open(f)); d["id"] = os.path.splitext(os.path.basename(f))[0]; rows.append(d)
q = json.load(open(qfile)); data = q.get("data", q)
quotes = {}
for sym, v in data.items():
    if v and v.get("close") is not None:
        t = sym.split(":")[1]
        quotes.setdefault(t, {"p": float(v["close"]), "chg": v.get("change"), "sym": sym})
out = {"actualizado": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "fuente": "TradingView", "rows": rows, "quotes": quotes}
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
json.dump(out, open(os.path.join(root, "posiciones.json"), "w"), ensure_ascii=False, indent=1)
print("ok", len(rows), "posiciones,", len(quotes), "precios")
