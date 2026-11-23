"""Fase 0 · pruebas del bot de papel (riesgo, hora, valoración, regresión).

    python3 -m unittest discover -s tests -v

Las marcadas como "HUECO" documentan problemas confirmados en la auditoría del 22-nov: hoy fallan a propósito
(expectedFailure). Cuando la Fase 1 o la Fase 2 los arreglen, pasarán y unittest avisará "unexpected success":
entonces se quita el @expectedFailure. Nada aquí cambia la conducta del bot.
"""
import json, sys, unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tools"))
sys.path.insert(0, str(RAIZ / "tests"))
import papel_bot as P  # noqa: E402


def lectura(px=7000.0, zg=6990.0, mp=7005.0, gt="14:00", tf2=(0, 0, 0)):
    """Lectura mínima del recolector: gamma positiva, PIN a 5 pts y escalera con IC δ0.20 de 5 pts."""
    lad = {"v": [["c", 0.20, 7020, 7025, 0.30, 0.40], ["p", 0.20, 6980, 6975, 0.30, 0.40]], "c": []}
    return {"px": px, "zg": zg, "gt": gt, "mp": mp, "mn": None, "techo": None, "piso": None, "cw": None, "pw": None,
            "tf2": list(tf2), "mom": [0, 0, 0], "rem": [px - 15, px + 15], "lad": lad}


def pos(tipo="tend", dire=1, entrada=2.5, ancho=10, n=1, legs=None):
    return {"id": "x", "tipo": tipo, "dir": dire, "entrada": entrada, "ancho": ancho, "n": n,
            "legs": legs or [("c", 7000, 1), ("c", 7010, -1)], "m_in": 600, "ajuste": 0.0, "max": 0, "min": 0}


class Riesgo(unittest.TestCase):
    def setUp(self):
        self.b = P.Bot("2026-11-20")

    def test_ctr_por_riesgo(self):
        self.assertEqual(self.b.ctr(2.0), 5)       # $200 por contrato → tope de 5
        self.assertEqual(self.b.ctr(3.0), 3)       # $1,000 // $300
        self.assertEqual(self.b.ctr(8.55), 1)      # venta de 10 con crédito 1.45

    def test_parametros_actuales_no_pasan_el_riesgo_por_contrato(self):
        pb = P.PB
        peor = max(pb["deb_max"] * pb["deb_ancho"] + pb["slip"], pb["ven_ancho"] * (1 - pb["ven_min"]) + pb["slip"], 5)
        self.assertLessEqual(peor * 100, pb["riesgo"])   # por eso el hueco de ctr() hoy no se activa

    @unittest.expectedFailure
    def test_HUECO_ctr_no_debe_forzar_un_contrato(self):
        self.assertEqual(self.b.ctr(12.0), 0)      # $1,200 por contrato > $1,000 de riesgo: no debería abrir

    def test_bp_uso(self):
        self.b.abiertas = [pos("tend", 1, 2.5, 10, 3)]
        self.assertAlmostEqual(self.b.bp_uso(), 750)
        self.b.abiertas = [pos("venta", 1, -1.5, 10, 1)]
        self.assertAlmostEqual(self.b.bp_uso(), 850)
        self.b.abiertas = [pos("pin", 0, -1.0, 5, 2)]
        self.assertAlmostEqual(self.b.bp_uso(), 800)

    def test_puede_limites(self):
        b = self.b
        self.assertTrue(b.puede(600, 1))
        b.ult_perdida = 595
        self.assertFalse(b.puede(600, 1))          # enfriamiento de 15 min
        b.ult_perdida = -999
        b.ult_cierre[-1] = 595
        self.assertFalse(b.puede(600, 1))          # 10 min antes de ir en contra
        b.ult_cierre = {}
        b.abiertas = [pos("tend", 1)]
        self.assertFalse(b.puede(600, 1))          # ya hay una en esa dirección
        b.abiertas = []
        b.n_ent = P.PB["max_ops"]
        self.assertFalse(b.puede(600, 1))

    def test_stop_dia_cerrado(self):
        self.b.ops = [{"pnl": -1500}]
        self.assertFalse(self.b.puede(600, 1))

    @unittest.expectedFailure
    def test_HUECO_stop_dia_con_lo_abierto(self):
        b = self.b
        b.ops = [{"pnl": -1400}]
        b.abiertas = [pos("venta", -1, -1.5, 10, 1)]      # puede perder $850 más
        self.assertFalse(b.puede(600, 1))          # −1,400 − 850 ya pasa el stop de −1,500

    def test_capital_limita_en_abrir(self):
        b = self.b
        b.abiertas = [pos("venta", -1, -1.5, 10, 4)]      # $3,400 de los $3,500
        r = lectura()
        b.abrir("10:00", r, b.ctx(r), "tend", 1, [("c", 7000, 1), ("c", 7010, -1)], 2.5, 10, "prueba")
        self.assertEqual(len(b.abiertas), 1)       # no alcanza el capital: no abre

    def _ic_tras_perdida(self):
        b = self.b
        b.ult_perdida = P.mins("14:00")
        r = lectura()
        b.buscar("14:05", r, b.ctx(r))
        return b

    def test_ruta_c_abre_ic(self):
        b = self.b
        r = lectura()
        b.buscar("14:05", r, b.ctx(r))
        self.assertEqual([p["tipo"] for p in b.abiertas], ["pin"])

    @unittest.expectedFailure
    def test_HUECO_ruta_c_respeta_enfriamiento(self):
        self.assertEqual(self._ic_tras_perdida().abiertas, [])   # 5 min después de una pérdida no debería abrir


class Hora(unittest.TestCase):
    def test_et_invierno(self):
        self.assertEqual(P.et("9:00"), "10:00")
        self.assertEqual(P.et("14:59"), "15:59")

    @unittest.expectedFailure
    def test_HUECO_et_verano(self):
        self.assertEqual(P.et("9:00", fecha="2027-03-15"), "11:00")   # Fase 2: ET real con zoneinfo

    @unittest.expectedFailure
    def test_HUECO_cierre_verano(self):
        self.assertEqual(P.cierre_cdmx("2027-03-15"), 14 * 60)        # en verano el 0DTE vence a las 14:00 CDMX

    def test_cierre_invierno_pb(self):
        self.assertEqual(P.PB["cierre"], 15 * 60)

    def test_minuto_repetido_se_ignora(self):
        b = P.Bot("2026-11-20")
        r = lectura()
        b.paso("8:31:05", r)
        b.paso("08:31:40", r)                      # mismo minuto de otro recolector
        self.assertEqual(len(b.ruta), 1)


class Valoracion(unittest.TestCase):
    def test_cadena_real(self):
        b, p = P.Bot("2026-11-20"), pos()
        cad = {("c", 7000): 5.0, ("c", 7010): 2.0}
        self.assertAlmostEqual(b.valor(p, "10:00", lectura(), cad), 3.0)
        self.assertEqual(p["marca"], "cadena")

    def test_falta_strike_queda_marcado_modelo(self):
        b, p = P.Bot("2026-11-20"), pos()
        b.valor(p, "10:00", lectura(), {("c", 7000): 5.0})
        self.assertEqual(p["marca"], "modelo")

    def test_medir_comisiones_y_natural(self):
        b = P.Bot("2026-11-20")
        p = pos("tend", 1, 2.55, 10, 3)
        p["natural"] = 2.70
        m = b.medir(p, 300, "TP")
        self.assertAlmostEqual(m["com"], 2 * 3 * P.COM_PATA * 2)          # 2 patas × 3 contratos × ida y vuelta
        self.assertEqual(m["pnl_entrada_nat"], 300 - 45)                   # pagar 0.15 más × 3 × 100
        m = b.medir(pos("pin", 0, -1.0, 5, 2, [("p", 1, 1), ("p", 2, -1), ("c", 3, -1), ("c", 4, 1)]), 200, "Vencimiento")
        self.assertAlmostEqual(m["com"], 4 * 2 * P.COM_PATA)               # expira: solo la entrada
        self.assertEqual(m["marca_salida"], "liquidación")


class Regresion(unittest.TestCase):
    def test_replay_igual_a_la_referencia(self):
        import golden
        ref = json.loads(golden.REF.read_text(encoding="utf-8"))["dias"]
        locales = [f for f in golden.dias_locales() if f in ref]
        if not locales:
            self.skipTest("sin historial local")
        self.assertEqual(golden.diferencias(ref, golden.actual(locales)), [])


if __name__ == "__main__":
    unittest.main()
