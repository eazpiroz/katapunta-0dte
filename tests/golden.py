"""Replay de referencia (Fase 0): la huella de cada operación del replay con el Playbook actual.

Cualquier cambio al bot que NO deba cambiar su conducta tiene que reproducir esta huella operación por operación.
    python3 tests/golden.py --guardar     # rehace tests/golden_replay.json (solo si el cambio de conducta fue aprobado)
    python3 tests/golden.py               # compara el replay actual contra la referencia
Necesita el historial local (tools/historial, no va al repo); los días que falten se saltan.
"""
import glob, json, sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tools"))
import papel_bot as P  # noqa: E402

REF = Path(__file__).resolve().parent / "golden_replay.json"
CAMPOS = ("clave", "tipo", "entra", "sale", "entrada", "salida", "n", "pnl", "motivo")


def dias_locales():
    return sorted({Path(x).name[:10] for x in glob.glob(str(P.HIST / "20??-??-??.jsonl*"))})


def huella(fecha):
    b = P.correr_dia(fecha)
    return {"pnl": b.pnl_dia(), "ops": [{k: o[k] for k in CAMPOS} for o in b.ops]}


def actual(fechas=None):
    return {f: huella(f) for f in (fechas or dias_locales())}


def diferencias(ref, act):
    out = []
    for f, h in act.items():
        if f not in ref:
            out.append(f"{f}: día nuevo, no está en la referencia")
            continue
        r = ref[f]
        if len(r["ops"]) != len(h["ops"]):
            out.append(f"{f}: {len(r['ops'])} operaciones en la referencia, {len(h['ops'])} ahora")
        for i, (a, b) in enumerate(zip(r["ops"], h["ops"])):
            dif = {k: (a[k], b[k]) for k in CAMPOS if a[k] != b[k]}
            if dif:
                out.append(f"{f} op {i + 1}: {dif}")
        if r["pnl"] != h["pnl"]:
            out.append(f"{f}: P&L {r['pnl']} → {h['pnl']}")
    return out


if __name__ == "__main__":
    act = actual()
    if "--guardar" in sys.argv:
        REF.write_text(json.dumps({"playbook": P.PB["version"], "dias": act}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Referencia guardada: {len(act)} días, {sum(len(h['ops']) for h in act.values())} operaciones, "
              f"P&L ${sum(h['pnl'] for h in act.values()):,.0f}")
    else:
        ref = json.loads(REF.read_text(encoding="utf-8"))["dias"]
        d = diferencias(ref, act)
        print("\n".join(d) if d else f"OK: {len(act)} días idénticos a la referencia")
        sys.exit(1 if d else 0)
