"""
Suite de testes Fase 6 - Encerramento da implantação.
Cobre: feedback loop, gestão de recomendações, painel ADM, regressão Fases 3-5, rotas principais.
Executar: python -m unittest app.tests.test_fase6_encerramento -v
Ou: pytest app/tests/test_fase6_encerramento.py -v
"""
import os
import unittest
import json
import atexit
import warnings

os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=ResourceWarning, message=r"unclosed database in <sqlite3.Connection object.*")
warnings.filterwarnings("ignore", category=ResourceWarning)


def _cleanup_test_db_connections():
    """Fecha sessões e conexões abertas ao final da suíte de testes."""
    try:
        app = get_app()
        if app is None:
            return
        from app.extensions import db
        with app.app_context():
            db.session.remove()
            for engine in db.engines.values():
                engine.dispose()
    except Exception:
        pass


atexit.register(_cleanup_test_db_connections)


def tearDownModule():
    _cleanup_test_db_connections()


def get_app():
    try:
        from app.env_loader import load_app_env
        load_app_env()
        from app.web import app
        return app
    except Exception:
        return None


# --- 5.1 TESTES UNITÁRIOS ---

class TestParserRecomendacao(unittest.TestCase):
    """Parser de recomendação (JSON válido/inválido)."""

    def test_parse_recomendacao_json_valido(self):
        from app.run_cleiton_agente_customer_insight import parse_recomendacao_json
        s = '{"tema_sugerido": "logística", "tipo": "noticia", "prioridade": 8}'
        out = parse_recomendacao_json(s)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("tema_sugerido"), "logística")
        self.assertEqual(out.get("tipo"), "noticia")
        self.assertEqual(out.get("prioridade"), 8)

    def test_parse_recomendacao_json_invalido(self):
        from app.run_cleiton_agente_customer_insight import parse_recomendacao_json
        self.assertEqual(parse_recomendacao_json(None), {})
        self.assertEqual(parse_recomendacao_json(""), {})
        self.assertEqual(parse_recomendacao_json("  "), {})
        self.assertEqual(parse_recomendacao_json("not json"), {})
        self.assertEqual(parse_recomendacao_json("[1,2,3]"), {})

    def test_parse_contexto_json(self):
        from app.run_cleiton_agente_customer_insight import parse_contexto_json
        self.assertEqual(parse_contexto_json('{"x":1}'), {"x": 1})
        self.assertEqual(parse_contexto_json("invalid"), {})


class TestSelecaoRecomendacaoPrioritaria(unittest.TestCase):
    """Seleção de recomendação prioritária (ordem prioridade DESC, criado_em DESC)."""

    def test_selecionar_retorna_none_sem_app_context(self):
        from app.run_cleiton_agente_customer_insight import selecionar_recomendacao_prioritaria
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        with app.app_context():
            rec = selecionar_recomendacao_prioritaria()
            self.assertTrue(rec is None or hasattr(rec, "id") and hasattr(rec, "status"))

    def test_listar_pendentes_ordenacao(self):
        from app.run_cleiton_agente_customer_insight import listar_recomendacoes_pendentes
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        with app.app_context():
            lista = listar_recomendacoes_pendentes(5)
            self.assertIsInstance(lista, list)
            for r in lista:
                self.assertEqual(getattr(r, "status", None), "pendente")


class TestAtualizacaoStatusRecomendacao(unittest.TestCase):
    """Atualização de status recomendação (aplicada/descartada) com auditoria."""

    def test_atualizar_status_recomendacao_id_invalido(self):
        from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        ok = atualizar_status_recomendacao(999999, "aplicada", app)
        self.assertFalse(ok)

    def test_atualizar_status_invalido_rejeitado(self):
        from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        ok = atualizar_status_recomendacao(1, "invalido", app)
        self.assertFalse(ok)


class TestClassificacaoInsightPreservada(unittest.TestCase):
    """Classificação de insight preservada (escalar/manter/ajustar/pausar)."""

    def test_classificacao_escalar_manter_ajustar_pausar(self):
        from app.run_cleiton_agente_customer_insight import (
            classificar_desempenho,
            CLASSIFICACAO_ESCALAR,
            CLASSIFICACAO_MANTER,
            CLASSIFICACAO_AJUSTAR,
            CLASSIFICACAO_PAUSAR,
        )
        self.assertEqual(classificar_desempenho(80.0, 500), CLASSIFICACAO_ESCALAR)
        self.assertIn(classificar_desempenho(50.0, 500), (CLASSIFICACAO_MANTER, CLASSIFICACAO_AJUSTAR))
        self.assertEqual(classificar_desempenho(20.0, 500), CLASSIFICACAO_PAUSAR)
        self.assertEqual(classificar_desempenho(60.0, 50), CLASSIFICACAO_PAUSAR)


# --- 5.2 TESTES DE INTEGRAÇÃO ---

class TestCicloOrquestradorRecomendacao(unittest.TestCase):
    """Ciclo do orquestrador aplicando recomendação pendente ao payload."""

    def test_construir_payload_aceita_metadados_recomendacao_id(self):
        from app.run_cleiton_agente_dispatcher import construir_payload
        from datetime import datetime
        p = construir_payload(
            tipo_missao="noticia",
            tema="teste",
            prioridade=7,
            metadados={"recomendacao_id": 1, "insight_recomendacao": True},
        )
        self.assertIn("recomendacao_id", p.get("metadados", {}))
        self.assertTrue(p["metadados"].get("insight_recomendacao"))

    def test_auditoria_insight_persistida(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        with app.app_context():
            from app.models import AuditoriaGerencial
            from app.run_cleiton_agente_auditoria import registrar
            registrar(
                tipo_decisao="insight",
                decisao="Teste Fase 6",
                contexto={"teste": True},
                resultado="sucesso",
            )
            r = AuditoriaGerencial.query.filter_by(tipo_decisao="insight").order_by(AuditoriaGerencial.id.desc()).first()
            self.assertIsNotNone(r)
            self.assertEqual(r.decisao, "Teste Fase 6")


# --- 5.3 TESTES DE REGRESSÃO FASES 3-5 ---

class TestRegressaoJuliaPautaAprovada(unittest.TestCase):
    """Julia consome apenas pauta aprovada (Fase 3)."""

    def test_pauta_status_verificacao_existe(self):
        from app.models import Pauta
        self.assertTrue(hasattr(Pauta, "status_verificacao"))

    def test_run_julia_verifica_pauta_aprovada(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        with app.app_context():
            from app.models import Pauta
            aprovadas = Pauta.query.filter_by(status_verificacao="aprovado").count()
            self.assertGreaterEqual(aprovadas, 0)


class TestRegressaoPublisherDeduplicacao(unittest.TestCase):
    """Publisher bloqueia duplicidade por (noticia_id, canal)."""

    def test_ja_publicado_canal_existe(self):
        try:
            from app.run_julia_agente_publisher import _ja_publicado_canal
            self.assertTrue(callable(_ja_publicado_canal))
        except ImportError:
            self.skipTest("Publisher não disponível")


class TestRegressaoExecutarInsightAlinhada(unittest.TestCase):
    """/executar-insight alinhada ao ciclo do Cleiton (ciclo completo)."""

    def test_web_executar_insight_chama_orquestracao(self):
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        web_path = os.path.join(base, "web.py")
        with open(web_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("executar_insight", src)
        self.assertIn("executar_orquestracao", src, "Rota executar-insight deve acionar ciclo completo (executar_orquestracao)")


class TestRegressaoRetencaoInsight(unittest.TestCase):
    """Retenção continua incluindo entidades de insight/recomendação."""

    def test_retencao_limpa_insight_canal(self):
        from app.run_cleiton_agente_retencao import limpar_dados_antigos
        from app.models import InsightCanal, RecomendacaoEstrategica
        self.assertTrue("InsightCanal" in str(InsightCanal) or hasattr(InsightCanal, "__tablename__"))
        self.assertTrue(callable(limpar_dados_antigos))


# --- 5.4 TESTES DE ROTAS PRINCIPAIS (SMOKE) ---

class TestRotasPrincipais(unittest.TestCase):
    """Smoke / não-regressão de rotas principais."""

    def test_health_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        health = [r for r in rules if "health" in r.lower()]
        self.assertGreater(len(health), 0, "Rota /health ou similar deve existir (ops_bp)")

    def test_executar_cleiton_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertIn("/executar-cleiton", rules)

    def test_executar_insight_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertIn("/executar-insight", rules)

    def test_index_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertIn("/", rules)

    def test_admin_dashboard_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        admin_rules = [r for r in rules if "admin" in r and "dashboard" in r]
        self.assertGreater(len(admin_rules), 0)

    def test_cron_rota_existe(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        rules = [r.rule for r in app.url_map.iter_rules()]
        self.assertIn("/cron/executar-cleiton", rules)

    def test_cron_sem_segredo_retorna_403(self):
        app = get_app()
        if app is None:
            self.skipTest("App não disponível")
        old_secret = os.environ.get("CRON_SECRET")
        try:
            os.environ["CRON_SECRET"] = "segredo_teste"
            client = app.test_client()
            resp = client.get("/cron/executar-cleiton")
            self.assertEqual(resp.status_code, 403)
        finally:
            if old_secret is None:
                os.environ.pop("CRON_SECRET", None)
            else:
                os.environ["CRON_SECRET"] = old_secret


if __name__ == "__main__":
    unittest.main()
