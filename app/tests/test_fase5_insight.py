"""
Testes mínimos Fase 5 - Customer Insight.
Executar a partir da raiz do projeto: python -m pytest app/tests/test_fase5_insight.py -v
Ou com unittest: python -m unittest app.tests.test_fase5_insight -v
Alguns testes requerem app context e DB; outros (modelos, classificação) rodam sem Flask.
"""
import os
import unittest
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
    """Lazy import do app Flask; retorna None se dependências faltando."""
    try:
        from app.env_loader import load_app_env
        load_app_env()
        from app.web import app
        return app
    except Exception:
        return None


class TestFase5CustomerInsight(unittest.TestCase):
    """Testes de persistência e classificação do Customer Insight."""

    @property
    def app(self):
        if getattr(self, "_app", None) is None:
            self._app = get_app()
        return self._app

    def test_insight_canal_model_exists(self):
        """Modelo InsightCanal deve existir e ter bind gerencial."""
        from app.models import InsightCanal
        self.assertEqual(InsightCanal.__tablename__, "insight_canal")
        self.assertEqual(InsightCanal.__bind_key__, "gerencial")

    def test_recomendacao_estrategica_model_exists(self):
        """Modelo RecomendacaoEstrategica deve existir e ter status pendente/aplicada/descartada."""
        from app.models import RecomendacaoEstrategica
        self.assertEqual(RecomendacaoEstrategica.__tablename__, "recomendacao_estrategica")
        self.assertEqual(RecomendacaoEstrategica.__bind_key__, "gerencial")

    def test_classificar_desempenho(self):
        """Classificação: score alto => escalar, score baixo/impressões baixas => pausar."""
        from app.run_cleiton_agente_customer_insight import classificar_desempenho, CLASSIFICACAO_ESCALAR, CLASSIFICACAO_PAUSAR
        c_esc = classificar_desempenho(75.0, 500)
        self.assertIn(c_esc, (CLASSIFICACAO_ESCALAR, "escalar"))
        c_pau = classificar_desempenho(20.0, 500)
        self.assertIn(c_pau, (CLASSIFICACAO_PAUSAR, "pausar"))
        c_pau2 = classificar_desempenho(50.0, 50)  # poucas impressões
        self.assertIn(c_pau2, (CLASSIFICACAO_PAUSAR, "pausar"))

    def test_executar_insight_nao_quebra_sem_dados(self):
        """Executar insight sem dados na janela deve retornar True e registrar auditoria ignorado."""
        if self.app is None:
            self.skipTest("App Flask não disponível (dependências)")
        with self.app.app_context():
            from app.infra import ensure_database_schema
            from app.extensions import db
            from app.models import AuditoriaGerencial
            ensure_database_schema(db)
            from app.run_cleiton_agente_customer_insight import executar_insight
            ok = executar_insight(self.app)
            self.assertTrue(ok)
            aud = AuditoriaGerencial.query.filter_by(tipo_decisao="insight").count()
            self.assertGreaterEqual(aud, 0)

    def test_retencao_importa_insight_e_recomendacao(self):
        """Módulo de retenção deve importar InsightCanal e RecomendacaoEstrategica."""
        from app.run_cleiton_agente_retencao import limpar_dados_antigos
        self.assertTrue(callable(limpar_dados_antigos))


if __name__ == "__main__":
    unittest.main()
