"""Fase 0 · ¿Cuánto infla el papel? Replay del bot con las mediciones nuevas (no cambia ninguna decisión).

Por día: P&L del papel como hoy (precio medio + deslizamiento), con comisiones reales, con la entrada al precio
natural (bid/ask de la escalera, solo días con cotizaciones), el peor momento posible del día
(P&L cerrado − pérdida máxima de lo abierto) contra el stop del día, y cuántas salidas se marcaron con el modelo.

    python3 tools/fase0_reporte.py            # tabla markdown de todos los días del historial local
"""
import glob, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import papel_bot as P  # noqa: E402


def filas():
    out = []
    for f in sorted({Path(x).name[:10] for x in glob.glob(str(P.HIST / "20??-??-??.jsonl*"))}):
        b = P.correr_dia(f)
        if not b.ops:
            out.append({"f": f, "n": 0})
            continue
        nat = [o["pnl_entrada_nat"] for o in b.ops if o["pnl_entrada_nat"] is not None]
        out.append({"f": f, "n": len(b.ops), "pnl": b.pnl_dia(), "com": sum(o["com"] for o in b.ops),
                    "neto": sum(o["pnl_neto"] for o in b.ops),
                    "nat": (sum(nat) - sum(o["com"] for o in b.ops if o["pnl_entrada_nat"] is not None)) if len(nat) == len(b.ops) else None,
                    "peor": b.peor, "modelo": sum(1 for o in b.ops if o["marca_salida"] == "modelo")})
    return out


def main():
    fs = filas()
    stop = P.PB["stop_dia"]
    print("| Día | Ops | Papel hoy | Comisiones | Neto | Neto con entrada natural | Peor momento posible | Salidas con modelo |\n"
          "|---|---|---|---|---|---|---|---|")
    T = {"pnl": 0, "com": 0, "neto": 0, "nat": 0, "nat_pnl": 0}
    for r in fs:
        if not r["n"]:
            print(f"| {r['f'][5:]} | 0 | | | | | | |")
            continue
        alerta = " ⚠" if r["peor"] < stop else ""
        nat = f"${r['nat']:,.0f}" if r["nat"] is not None else "sin cotizaciones"
        print(f"| {r['f'][5:]} | {r['n']} | ${r['pnl']:,.0f} | ${r['com']:,.0f} | ${r['neto']:,.0f} | {nat} | "
              f"${r['peor']:,.0f}{alerta} | {r['modelo']} de {r['n']} |")
        for k in ("pnl", "com", "neto"):
            T[k] += r[k]
        if r["nat"] is not None:
            T["nat"] += r["nat"]; T["nat_pnl"] += r["pnl"]
    n = sum(r["n"] for r in fs)
    print(f"| **Total** | {n} | ${T['pnl']:,.0f} | ${T['com']:,.0f} | ${T['neto']:,.0f} | ${T['nat']:,.0f} (papel ${T['nat_pnl']:,.0f} esos días) | "
          f"{min((r['peor'] for r in fs if r['n']), default=0):,.0f} | {sum(r.get('modelo', 0) for r in fs)} de {n} |")
    print(f"\nComisión: ${P.COM_PATA} por contrato por pata (mediana real). ⚠ = el peor momento pasó el stop del día (${stop:,.0f}). "
          "La salida sigue siendo precio medio o modelo: el historial no guarda la cadena completa por minuto.")


if __name__ == "__main__":
    main()
