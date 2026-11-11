"""Monitor Posiciones: alertas al celular a partir de la foto horaria.
Uso: python3 tools/posiciones_alertas.py DIR_DOCS QUOTES.json
Lee las posiciones (un JSON por documento) y los precios de TradingView, revisa
TP/SL, niveles a vigilar ("vigilar") y recordatorios con fecha ("recordar").
Guarda el estado en alertas_estado.json (raíz del repo) para avisar solo lo NUEVO.
Imprime las alertas nuevas (una por línea) o nada si no hay."""
import json, sys, glob, os, datetime
from zoneinfo import ZoneInfo

docs_dir, qfile = sys.argv[1], sys.argv[2]
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
state_path = os.path.join(root, "alertas_estado.json")
hoy = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()

q = json.load(open(qfile)); data = q.get("data", q)
px = {}
for sym, v in data.items():
    if v and v.get("close") is not None:
        px.setdefault(sym.split(":")[1], float(v["close"]))

try: state = json.load(open(state_path))
except Exception: state = {"activas": [], "recordados": []}
activas_prev = set(state.get("activas", [])); recordados = set(state.get("recordados", []))

def n(v):
    try: return None if v in (None, "") else float(v)
    except Exception: return None
def f(v): return f"{v:,.2f}"

ahora = {}   # key -> texto
for path in sorted(glob.glob(os.path.join(docs_dir, "*.json"))):
    d = json.load(open(path)); pid = os.path.splitext(os.path.basename(path))[0]
    if d.get("etapa") != "abierta": continue
    t = (d.get("ticker") or "").upper(); p = px.get(t)
    nombre = t + (" " + d["strikes"] if d.get("strikes") else "")
    # recordatorios con fecha (una sola vez)
    for r in d.get("recordar") or []:
        k = f"{pid}|rec|{r.get('fecha')}|{r.get('texto')}"
        if r.get("fecha") and r["fecha"] <= hoy and k not in recordados:
            ahora[k] = f"📅 {nombre}: {r.get('texto')}"
    if p is None: continue
    tp, sl = n(d.get("tp")), n(d.get("sl"))
    abajo = d.get("slAbajo") is not False if d.get("instrumento") == "opcion" else True
    if sl is not None:
        if (p <= sl) if abajo else (p >= sl):
            ahora[f"{pid}|sl"] = f"🔴 {nombre}: SL {f(sl)} tocado (precio {f(p)})"
        elif (p <= sl * 1.02) if abajo else (p >= sl * 0.98):
            ahora[f"{pid}|slcerca"] = f"🟠 {nombre}: a menos de 2% del SL {f(sl)} (precio {f(p)})"
    if tp is not None:
        if p >= tp:
            ahora[f"{pid}|tp"] = f"🟢 {nombre}: TP {f(tp)} alcanzado (precio {f(p)})"
        elif p >= tp * 0.97:
            ahora[f"{pid}|tpcerca"] = f"🟢 {nombre}: a menos de 3% del TP {f(tp)} (precio {f(p)})"
    for i, v in enumerate(d.get("vigilar") or []):
        lv = n(v.get("precio")); arriba = v.get("cruce", "arriba") == "arriba"
        if lv is None: continue
        if (p >= lv) if arriba else (p <= lv):
            ahora[f"{pid}|vig{i}|{lv}"] = f"🔔 {nombre} {'≥' if arriba else '≤'} {f(lv)} (precio {f(p)}): {v.get('texto','')}"

nuevas = [k for k in ahora if k not in activas_prev and k not in recordados]
for k in nuevas:
    print(ahora[k])
    if "|rec|" in k: recordados.add(k)
state = {"actualizado": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "activas": sorted(k for k in ahora if "|rec|" not in k),
         "recordados": sorted(recordados)}
json.dump(state, open(state_path, "w"), ensure_ascii=False, indent=1)
