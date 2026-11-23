"""23-nov: la API de datos de Schwab respondió 500 desde la apertura. Pruebas sin red de:
reintentos, modo degradado del recolector, respaldo del tape con el SPX del streaming, apagado del tape,
y que el bot no abra nada nuevo con una lectura degradada pero siga gestionando lo abierto.

    python3 -m unittest tests.test_schwab_caido -v
"""
import io, json, sys, tempfile, time, unittest, urllib.error
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "tools"))
import flightdeck_recolector as R  # noqa: E402
import tape_schwab as TS  # noqa: E402
import papel_bot as P  # noqa: E402


def http500():
    return urllib.error.HTTPError("https://api", 500, "Internal Server Error", {}, io.BytesIO(b""))


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Reintentos(unittest.TestCase):
    def test_500_dos_veces_y_luego_responde(self):
        llamadas = []

        def urlopen(req, timeout):
            llamadas.append(1)
            if len(llamadas) < 3:
                raise http500()
            return Resp(b'{"ok": 1}')
        with mock.patch.object(R, "access", return_value="x"), mock.patch.object(R.time, "sleep"), \
                mock.patch.object(R.urllib.request, "urlopen", urlopen):
            self.assertEqual(R.sget({}, "/quotes", symbols="$SPX"), {"ok": 1})
        self.assertEqual(len(llamadas), 3)

    def test_500_persistente_se_rinde_tras_3(self):
        n = []

        def urlopen(req, timeout):
            n.append(1)
            raise http500()
        with mock.patch.object(R, "access", return_value="x"), mock.patch.object(R.time, "sleep"), \
                mock.patch.object(R.urllib.request, "urlopen", urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                R.sget({}, "/quotes")
        self.assertEqual(len(n), 3)

    def test_401_no_se_reintenta(self):
        n = []

        def urlopen(req, timeout):
            n.append(1)
            raise urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b""))
        with mock.patch.object(R, "access", return_value="x"), mock.patch.object(R.time, "sleep"), \
                mock.patch.object(R.urllib.request, "urlopen", urlopen):
            with self.assertRaises(urllib.error.HTTPError):
                R.sget({}, "/quotes")
        self.assertEqual(len(n), 1)


class Degradado(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        h = Path(self.tmp.name)
        self.p = [mock.patch.object(R, "HIST", h), mock.patch.object(R, "SPOT_STREAM", h / "spot_stream.json"),
                  mock.patch.object(R, "gh_put", return_value=True), mock.patch.object(R, "gh_manual", return_value={})]
        for x in self.p:
            x.start()
        R.ULT.clear()
        self.h = h

    def tearDown(self):
        for x in self.p:
            x.stop()
        R.ULT.clear()
        self.tmp.cleanup()

    def base(self):
        return {"o": [1], "h": [1], "l": [1], "c": [1], "v": [], "pre": [], "rows": [],
                "read": {"px": 7000.0, "t": "8:24", "zg": 6990, "mp": 7020, "mn": 6950, "strad": 40.0,
                         "lad": {"v": [["c", 0.2, 7020, 7030, 1.4, 1.5]], "c": []}, "tf2": [1, 1, 0]}}

    def test_sin_respaldo_no_publica(self):
        R.ULT["SPX"] = self.base()
        self.assertFalse(R.degradado("tok", 500))

    def test_respaldo_viejo_no_se_usa(self):
        R.ULT["SPX"] = self.base()
        R.SPOT_STREAM.write_text(json.dumps({"px": 7061.5, "epoch": time.time() - 600}))
        self.assertFalse(R.degradado("tok", 500))

    def test_publica_con_precio_del_streaming_y_escalera_vacia(self):
        R.ULT["SPX"] = self.base()
        R.SPOT_STREAM.write_text(json.dumps({"px": 7061.5, "epoch": time.time()}))
        self.assertTrue(R.degradado("tok", 500))
        hoy = R.now().date().isoformat()
        r = json.loads((self.h / f"{hoy}.jsonl").read_text().splitlines()[-1])["SPX"]
        self.assertEqual(r["px"], 7061.5)
        self.assertEqual(r["lad"], {"v": [], "c": []})
        self.assertIn("Schwab 500", r["degradado"])
        self.assertEqual(r["rem"], [7021.5, 7101.5])
        self.assertEqual(R.ULT["SPX"]["read"]["px"], 7000.0)       # la última buena no se pisa

    def test_tras_reinicio_usa_el_historial(self):
        hoy = R.now().date().isoformat()
        (self.h / f"{hoy}.jsonl").write_text(json.dumps({"ts": "08:24:01", "SPX": self.base()["read"]}) + "\n")
        R.SPOT_STREAM.write_text(json.dumps({"px": 7050.0, "epoch": time.time()}))
        self.assertTrue(R.degradado("tok", 500))

    def test_lectura_manual_se_actualiza(self):
        hoy = R.now().date().isoformat()
        with mock.patch.object(R, "gh_manual", return_value={"date": hoy, "SPX": {"reads": [{"t": "8:40", "zg": 7040, "mp": 7100, "mn": 7000}]}}):
            out = R.lectura_degradada(self.base(), 7061.5, 500, "tok", R.now())
        self.assertEqual((out["read"]["zg"], out["read"]["gt"]), (7040, "8:40"))


class Tape(unittest.TestCase):
    def test_leer_spx_del_streaming(self):
        sp = {"px": None, "t": 0.0, "ult": None}
        TS.leer_spx(sp, {"data": [{"service": "LEVELONE_EQUITIES", "content": [{"key": "$SPX", "3": 7061.25}]},
                                  {"service": "LEVELONE_OPTIONS", "content": [{"key": "x", "3": 1.0}]}]})
        self.assertEqual(sp["px"], 7061.25)

    def test_strikes_con_respaldo_si_quotes_cae(self):
        with mock.patch.object(R, "sget", side_effect=http500()):
            spot, syms = TS.strikes({}, "2026-11-23", 7061.0)
            self.assertEqual(spot, 7061.0)
            self.assertTrue(syms)
            with self.assertRaises(urllib.error.HTTPError):
                TS.strikes({}, "2026-11-23")

    def test_guardar_spx_y_leer_respaldo(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(R, "HIST", Path(d)), \
                mock.patch.object(R, "SPOT_STREAM", Path(d) / "spot_stream.json"):
            TS.guardar_spx({"px": 7055.0, "t": time.time()})
            self.assertEqual(R.spot_respaldo(), 7055.0)

    def test_prueba_original_del_tape(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            TS.prueba()


class BotConLecturaDegradada(unittest.TestCase):
    def test_no_abre_y_si_gestiona(self):
        b = P.Bot("2026-11-23")
        r = {"px": 7000.0, "zg": 7010.0, "gt": "8:40", "mp": 7020.0, "mn": 6990.0, "techo": None, "piso": None, "cw": None,
             "pw": None, "tf2": [-1, -1, 0], "mom": [-1, -1, 0], "rem": [6960, 7040], "strad": 40.0, "lad": {"v": [], "c": []}}
        b.paso("09:30:01", r)
        self.assertEqual(b.abiertas, [])                          # sin escalera no abre (A pediría δ0.30)
        b.abiertas = [{"id": "x", "tipo": "tend", "dir": -1, "entrada": 2.5, "ancho": 10, "n": 1,
                       "legs": [("p", 7000, 1), ("p", 6990, -1)], "m_in": 560, "ajuste": 0.0, "max": 0, "min": 0,
                       "t_in": "9:20", "regla": "A", "ctx": {}, "obj": 6950.0, "ancla": None, "estimado": False}]
        b.paso("09:31:01", dict(r, px=6940.0))                    # llega al objetivo con precio del respaldo
        self.assertEqual(len(b.ops), 1)
        self.assertTrue(b.ops[0]["motivo"].startswith("TP"))


class FinDelTape(unittest.TestCase):
    def test_el_ciclo_interno_revisa_la_hora_de_fin(self):
        src = (RAIZ / "tools" / "tape_schwab.py").read_text(encoding="utf-8")
        i = src.index("            while True:\n                if R.now() >= fin:")
        self.assertGreater(i, src.index("def run():"))


if __name__ == "__main__":
    unittest.main()
