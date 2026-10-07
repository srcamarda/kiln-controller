"""
test_curves.py — Validação, carga tolerante e gravação atômica de curvas.
Rodar:  python3 -m unittest test_curves -v
"""

import copy
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import controller
import curves_manager as cm

GOOD = {"id": "c1", "name": "Biscoito", "soak_minutes": 20,
        "segments": [{"rate_per_hour": 100, "target_temp": 600},
                     {"rate_per_hour": 150, "target_temp": 900}]}


def curve(**kw):
    c = copy.deepcopy(GOOD)
    c.update(kw)
    return c


class TestValidacao(unittest.TestCase):

    def test_valida(self):
        self.assertEqual(cm.validate_curve(GOOD), "")

    def test_casos_invalidos(self):
        casos = [
            "texto",
            curve(name=""),
            curve(name="x" * (cm.NAME_MAX_LEN + 1)),
            curve(id=""),
            curve(segments=[]),
            curve(segments=[{"rate_per_hour": 100}]),
            curve(segments=[{"rate_per_hour": "100", "target_temp": 600}]),
            curve(segments=[{"rate_per_hour": True, "target_temp": 600}]),
            curve(segments=[{"rate_per_hour": 5, "target_temp": 600}]),
            curve(segments=[{"rate_per_hour": 100, "target_temp": cm.TARGET_MAX + 1}]),
            curve(segments=[{"rate_per_hour": 100, "target_temp": 600},
                            {"rate_per_hour": 100, "target_temp": 500}]),
            curve(segments=[{"rate_per_hour": 100, "target_temp": 200 + i * 50}
                            for i in range(cm.MAX_SEGMENTS + 1)]),
            curve(soak_minutes=0),
            curve(soak_minutes=None),
        ]
        for c in casos:
            self.assertNotEqual(cm.validate_curve(c), "", c)

    def test_limite_com_folga_para_sobretemperatura(self):
        self.assertGreaterEqual(controller.OVERTEMP_ABS - cm.TARGET_MAX, 25)

    def test_curvas_do_repositorio_validas(self):
        curves, warnings = cm.load_curves()
        self.assertEqual(warnings, [])
        self.assertGreater(len(curves), 0)


class TestCarga(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "curves.json")

    def write(self, path, content):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def test_ignora_curva_invalida(self):
        self.write(self.path, [GOOD, curve(id="c2", segments=[])])
        curves, warnings = cm.load_curves(self.path)
        self.assertEqual([c["id"] for c in curves], ["c1"])
        self.assertTrue(any("ignorada" in w for w in warnings))

    def test_corrige_id_repetido_e_ausente(self):
        sem_id = curve(name="Outra")
        del sem_id["id"]
        self.write(self.path, [GOOD, curve(name="Cópia"), sem_id])
        curves, _ = cm.load_curves(self.path)
        ids = [c["id"] for c in curves]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(curves), 3)

    def test_arquivo_corrompido_usa_backup(self):
        self.write(self.path, '[{"id": "c1", "name": "Bisc')       # cortado no meio
        self.write(self.path + ".bak", [GOOD])
        curves, warnings = cm.load_curves(self.path)
        self.assertEqual(len(curves), 1)
        self.assertTrue(any("backup" in w for w in warnings))

    def test_sem_nada_lista_vazia(self):
        curves, warnings = cm.load_curves(self.path)
        self.assertEqual(curves, [])
        self.assertTrue(warnings)

    def test_formato_errado(self):
        self.write(self.path, {"curvas": []})
        curves, warnings = cm.load_curves(self.path)
        self.assertEqual(curves, [])


class TestGravacao(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "curves.json")
        cm.save_curves([GOOD], self.path)

    def test_grava_e_cria_backup(self):
        cm.save_curves([GOOD, curve(id="c2", name="Nova")], self.path)
        curves, _ = cm.load_curves(self.path)
        self.assertEqual(len(curves), 2)
        with open(self.path + ".bak", encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)), 1)          # versão anterior
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_queda_durante_gravacao_nao_corrompe(self):
        with mock.patch.object(cm.os, "replace", side_effect=OSError("energia caiu")):
            with self.assertRaises(OSError):
                cm.save_curves([GOOD, curve(id="c2", name="Nova")], self.path)
        curves, warnings = cm.load_curves(self.path)
        self.assertEqual(len(curves), 1)                    # original intacto
        self.assertEqual(warnings, [])

    def test_recusa_invalida(self):
        with self.assertRaises(ValueError):
            cm.save_curves([GOOD, curve(id="c2", segments=[])], self.path)
        with self.assertRaises(ValueError):
            cm.save_curves([GOOD, curve(name="Outra")], self.path)   # id repetido
        self.assertEqual(len(cm.load_curves(self.path)[0]), 1)

    def test_backup_bom_nao_e_sobrescrito_por_arquivo_corrompido(self):
        cm.save_curves([GOOD, curve(id="c2", name="Nova")], self.path)   # .bak = [GOOD]
        with open(self.path, "w") as f:
            f.write("lixo")
        cm.save_curves([curve(id="c3", name="Terceira")], self.path)
        with open(self.path + ".bak", encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["id"], "c1")


class TestCriacao(unittest.TestCase):

    def test_nome_e_id_unicos(self):
        curves = [curve(id="curva_3", name="Curva 3"), curve(id="curva_4", name="Outra")]
        name = cm.next_curve_name(curves)
        self.assertEqual(name, "Curva 4")
        tpl = cm.new_curve_template("Curva 4", curves)
        self.assertNotIn(tpl["id"], {c["id"] for c in curves})


# ── Interface (main) ────────────────────────────────────────────────────────

def _import_main():
    try:
        import PIL  # noqa: F401
    except ImportError:
        pil = types.ModuleType("PIL")
        for n in ("Image", "ImageDraw", "ImageFont"):
            setattr(pil, n, mock.MagicMock())
            sys.modules["PIL." + n] = getattr(pil, n)
        sys.modules["PIL"] = pil
    import main
    return main


class TestInterface(unittest.TestCase):

    def setUp(self):
        self.main = _import_main()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "curves.json")
        cm.save_curves([GOOD], self.path)
        p = mock.patch.object(cm, "CURVES_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def _app(self, curves):
        m = self.main
        app = m.KilnApp.__new__(m.KilnApp)
        app.curves = curves
        app.ui = m.UIState.MENU
        app.msg_title = app.msg_text = ""
        app.msg_return = m.UIState.MENU
        app.edit_curve = None
        app.editing_existing = False
        return app

    def test_menu_sem_curvas(self):
        app = self._app([])
        for sel in (0, 3):
            app.ui = self.main.UIState.MENU
            app.menu_sel = sel
            app._menu_confirm()
            self.assertEqual(app.ui, self.main.UIState.MESSAGE)
            self.assertEqual(app.msg_title, "SEM CURVAS")

    def test_salvar_invalida_mostra_aviso(self):
        app = self._app([GOOD])
        app.edit_curve = curve(id="c2", name="Nova", soak_minutes=0)
        app._save_curve()
        self.assertEqual(app.ui, self.main.UIState.MESSAGE)
        self.assertEqual(app.msg_return, self.main.UIState.CURVE_SAVE)
        self.assertEqual(len(app.curves), 1)

    def test_falha_de_disco_mostra_aviso_e_nao_altera_lista(self):
        app = self._app([GOOD])
        app.edit_curve = curve(id="c2", name="Nova")
        with mock.patch.object(self.main, "save_curves", side_effect=OSError("disco cheio")):
            app._save_curve()
        self.assertEqual(app.ui, self.main.UIState.MESSAGE)
        self.assertEqual(len(app.curves), 1)

    def test_salvar_edicao_substitui_pelo_id(self):
        app = self._app([GOOD, curve(id="c2", name="Outra")])
        app.editing_existing = True
        app.edit_curve = curve(id="c2", name="Outra editada")
        app._save_curve()
        self.assertEqual([c["name"] for c in app.curves], ["Biscoito", "Outra editada"])
        self.assertEqual(len(cm.load_curves(self.path)[0]), 2)


if __name__ == "__main__":
    unittest.main()
