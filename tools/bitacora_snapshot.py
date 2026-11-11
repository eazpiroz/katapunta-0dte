"""Arma bitacora.json (foto pública de la Bitácora, solo lectura).
Uso: python3 tools/bitacora_snapshot.py DIR_BITACORA DIR_DIAS
Cada carpeta tiene un JSON por documento (nombre = id) de las colecciones
"bitacora" y "dias". Escribe bitacora.json en la raíz del repositorio."""
import json, sys, glob, os, datetime
ops_dir, dias_dir = sys.argv[1], sys.argv[2]
ops = []
for f in sorted(glob.glob(os.path.join(ops_dir, "*.json"))):
    d = json.load(open(f)); d["id"] = os.path.splitext(os.path.basename(f))[0]; ops.append(d)
dias = {}
for f in sorted(glob.glob(os.path.join(dias_dir, "*.json"))):
    dias[os.path.splitext(os.path.basename(f))[0]] = json.load(open(f))
out = {"actualizado": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "ops": ops, "dias": dias}
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
json.dump(out, open(os.path.join(root, "bitacora.json"), "w"), ensure_ascii=False, indent=1)
print("ok", len(ops), "operaciones,", len(dias), "días")
