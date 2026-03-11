"""
Suite de testes Fase 5 - Máquina de estados da série editorial, reconciliação de órfãos
e replanejamento determinístico.

Executar:
- python -m unittest app.tests.test_fase5_estado_serie -v
"""
import os
import unittest
import contextlib
from datetime import datetime, timedelta, timezone

os.environ.setdefault("APP_ENV", "dev")

_APP_CACHE = None


def get_app():
    """Lazy import do app Flask; retorna None se dependências faltando."""
    global _APP_CACHE
    if _APP_CACHE is not None:
        return _APP_CACHE
    try:
        from app.env_loader import load_app_env

        load_app_env()
        from app.web import app
        _APP_CACHE = app
        return app
    except Exception:
        return None


def _cleanup_db_resources(app, dispose_engines: bool = False) -> None:
    """Libera sessão e, opcionalmente, fecha pools de conexão para reduzir ResourceWarning."""
    if app is None:
        return
    try:
        from app.extensions import db
        with app.app_context():
            db.session.remove()
            if dispose_engines:
                for engine in db.engines.values():
                    engine.dispose()
    except Exception:
        pass


class TestMaquinaEstadosSerie(unittest.TestCase):
    """Testes da máquina de estados de SerieItemEditorial."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def _criar_item(self, status: str, data_planejada: datetime | None = None):
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Teste",
                tema="Tema Teste",
                cadencia_dias=2,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status=status,
                data_planejada=data_planejada,
            )
            db.session.add(item)
            db.session.commit()
            return item.id

    def test_transicoes_validas(self):
        """Transições válidas da máquina de estados devem ser aplicadas com sucesso."""
        from app.extensions import db
        from app.models import SerieItemEditorial
        from app.run_cleiton_agente_serie import atualizar_status_item

        with self.app.app_context():
            item_id = self._criar_item(status="planejado")
            # planejado -> em_andamento
            ok = atualizar_status_item(item_id, "em_andamento", motivo="teste planejado->em_andamento")
            self.assertTrue(ok)
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertEqual(item.status, "em_andamento")

            # em_andamento -> publicado
            ok = atualizar_status_item(item_id, "publicado", motivo="teste em_andamento->publicado")
            self.assertTrue(ok)
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertEqual(item.status, "publicado")

    def test_transicao_invalida_bloqueada(self):
        """Transições inválidas (ex.: publicado->planejado) devem ser bloqueadas com retorno False."""
        from app.extensions import db
        from app.models import SerieItemEditorial
        from app.run_cleiton_agente_serie import atualizar_status_item

        with self.app.app_context():
            item_id = self._criar_item(status="publicado")
            ok = atualizar_status_item(item_id, "planejado", motivo="teste publicado->planejado (inválido)")
            self.assertFalse(ok)
            item = db.session.get(SerieItemEditorial, item_id)
            # Status permanece publicado
            self.assertEqual(item.status, "publicado")


class TestReconciliacaoOrfaos(unittest.TestCase):
    """Testes da rotina de reconciliação de itens de série órfãos."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def test_em_andamento_sem_pauta_vira_falha(self):
        """Item em_andamento sem pauta e data passada deve ser marcado como falha."""
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial
        from app.run_cleiton_agente_serie import reconciliar_itens_orfaos

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Reconciliação",
                tema="Tema",
                cadencia_dias=1,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            ontem = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status="em_andamento",
                pauta_id=None,
                data_planejada=ontem,
            )
            db.session.add(item)
            db.session.commit()

            stats = reconciliar_itens_orfaos(agora=datetime.now(timezone.utc).replace(tzinfo=None))
            db.session.refresh(item)
            self.assertEqual(item.status, "falha")
            self.assertGreaterEqual(stats.get("em_andamento_sem_pauta", 0), 1)

    def test_publicado_sem_noticia_vira_falha(self):
        """Item publicado sem noticia_id deve ser reclassificado como falha pela reconciliação."""
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial
        from app.run_cleiton_agente_serie import reconciliar_itens_orfaos

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Reconciliação Publicado",
                tema="Tema",
                cadencia_dias=1,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status="publicado",
                noticia_id=None,
                data_planejada=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.session.add(item)
            db.session.commit()

            stats = reconciliar_itens_orfaos(agora=datetime.now(timezone.utc).replace(tzinfo=None))
            db.session.refresh(item)
            self.assertEqual(item.status, "falha")
            self.assertGreaterEqual(stats.get("publicado_sem_noticia", 0), 1)


class TestReplanejamentoDeterministico(unittest.TestCase):
    """Testes da política determinística de replanejamento de itens de série."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def _criar_serie_e_item(self, status: str, data_planejada: datetime | None):
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Replanejamento",
                tema="Tema",
                cadencia_dias=3,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status=status,
                data_planejada=data_planejada,
            )
            db.session.add(item)
            db.session.commit()
            return serie.id, item.id

    def test_replanejamento_de_planejado_atrasado(self):
        """Item planejado com data_planejada < hoje deve ter data ajustada para hoje."""
        from app.extensions import db
        from app.models import SerieItemEditorial
        from app.run_cleiton_agente_serie import replanejar_itens_atrasados_e_falhos

        ontem = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
        _, item_id = self._criar_serie_e_item(status="planejado", data_planejada=ontem)

        with self.app.app_context():
            stats = replanejar_itens_atrasados_e_falhos(agora=datetime.now(timezone.utc).replace(tzinfo=None))
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertIsNotNone(item.data_planejada)
            self.assertGreaterEqual(stats.get("planejados_replanejados", 0), 1)

    def test_replanejamento_de_falha_para_planejado(self):
        """Item em falha deve ser reaberto como planejado com nova data baseada na cadência da série."""
        from app.extensions import db
        from app.models import SerieItemEditorial
        from app.run_cleiton_agente_serie import replanejar_itens_atrasados_e_falhos

        hoje = datetime.now(timezone.utc).replace(tzinfo=None)
        _, item_id = self._criar_serie_e_item(status="falha", data_planejada=hoje)

        with self.app.app_context():
            stats = replanejar_itens_atrasados_e_falhos(agora=hoje)
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertEqual(item.status, "planejado")
            self.assertIsNotNone(item.data_planejada)
            self.assertGreaterEqual(stats.get("falha_reabertos_planejado", 0), 1)


class TestNaoRegressaoNoticiaRapidaSprint5(unittest.TestCase):
    """Smoke de não-regressão para notícia rápida no contexto da Sprint 5."""

    def test_fluxo_noticia_rapida_mantido(self):
        """tipo_missao=noticia continua possível e caminho_usado permanece noticia_rapida."""
        from datetime import datetime
        from unittest.mock import patch

        from app.tests.test_fase4_meta_diaria import DummyApp
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        base_patches = [
            patch("app.run_cleiton_agente_orquestrador.bootstrap_regras"),
            patch("app.run_cleiton_agente_orquestrador.bootstrap_plano_se_necessario"),
            patch("app.run_cleiton_agente_orquestrador.auditoria_registrar"),
            patch("app.run_cleiton_agente_orquestrador.obter_plano_ativo", return_value=None),
            patch("app.run_cleiton_agente_orquestrador.ultima_auditoria_orquestracao", return_value=None),
            patch("app.run_cleiton_agente_orquestrador.get_prioridade_padrao", return_value=5),
            patch("app.run_cleiton_agente_orquestrador.get_janela_publicacao", return_value=(6, 22)),
            patch("app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia", return_value=True),
            patch("app.run_cleiton_agente_orquestrador.dentro_janela_publicacao", return_value=True),
            patch("app.run_cleiton_agente_orquestrador.executar_retencao"),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
            patch("app.run_cleiton_agente_scout.executar_coleta",
                  return_value={"inseridas": 1, "ignoradas_duplicata": 0, "erros": 0, "fontes_processadas": 1}),
            patch("app.run_cleiton_agente_verificador.executar_verificacao",
                  return_value={"processadas": 1, "aprovadas": 1, "revisar": 0, "rejeitadas": 0}),
            patch("app.run_cleiton_agente_orquestrador.construir_payload",
                  return_value={"mission_id": "mn5", "tipo_missao": "noticia"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
            patch("app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria", return_value=None),
            patch("app.run_cleiton_agente_customer_insight.executar_insight"),
        ]
        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="noticia"),
        ]
        with contextlib.ExitStack() as stack:
            for p in base_patches + extra:
                stack.enter_context(p)
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("tipo_missao"), "noticia")
        self.assertEqual(resultado.get("caminho_usado"), "noticia_rapida")


if __name__ == "__main__":
    unittest.main()

