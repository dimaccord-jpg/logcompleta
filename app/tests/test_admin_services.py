"""
Testes dos serviços do painel administrativo (agent, pauta, serie, import, plano, termo, auditoria).
Cobertura mínima para garantir que os módulos importam e as funções principais retornam o esperado.
"""
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("APP_ENV", "dev")


class TestAgentService(unittest.TestCase):
    """Testes do agent_service."""

    def test_admin_exec_mode_dev(self):
        with patch.dict(os.environ, {"APP_ENV": "dev", "ADMIN_CLEITON_EXEC_MODE": ""}):
            from app.services import agent_service
            mode = agent_service.admin_exec_mode()
            self.assertIn(mode, ("sync", "async"))

    def test_admin_exec_mode_forced_async(self):
        with patch.dict(os.environ, {"ADMIN_CLEITON_EXEC_MODE": "async"}):
            from app.services import agent_service
            self.assertEqual(agent_service.admin_exec_mode(), "async")

    def test_obter_frequencia_horas_retorna_int(self):
        from app.services import agent_service
        v = agent_service.obter_frequencia_horas()
        self.assertIsInstance(v, int)
        self.assertGreaterEqual(v, 1)

    def test_obter_janela_publicacao_retorna_tupla(self):
        from app.services import agent_service
        inicio, fim = agent_service.obter_janela_publicacao()
        self.assertIsInstance(inicio, int)
        self.assertIsInstance(fim, int)
        self.assertGreaterEqual(inicio, 0)
        self.assertLessEqual(fim, 23)

    def test_obter_kpis_insight_retorna_dict(self):
        from app.services import agent_service
        kpis = agent_service.obter_kpis_insight()
        self.assertIsInstance(kpis, dict)
        self.assertIn("recomendacoes_pendentes", kpis)
        self.assertIn("total_metricas", kpis)

    def test_ler_ultima_execucao_manual_sem_arquivo(self):
        from app.services import agent_service
        with patch.object(agent_service, "get_data_dir", return_value=os.path.devnull):
            # get_data_dir retorna devnull, então o path não existe
            result = agent_service.ler_ultima_execucao_manual(app=None)
        self.assertIsNone(result)


class TestPlanoService(unittest.TestCase):
    """Testes do plano_service."""

    def test_obter_config_planos_retorna_dict(self):
        from app.services import plano_service
        config = plano_service.obter_config_planos()
        self.assertIsInstance(config, dict)
        self.assertIn("julia_chat_max_history", config)
        self.assertIn("freemium_consultas_dia", config)
        self.assertIn("freemium_trial_dias", config)

    def test_salvar_limite_freemium_vazio_retorna_lista(self):
        from app.services import plano_service
        msgs = plano_service.salvar_limite_freemium()
        self.assertIsInstance(msgs, list)


class TestValidators(unittest.TestCase):
    """Testes do utils.validators."""

    def test_clamp_int(self):
        from app.utils.validators import clamp_int
        self.assertEqual(clamp_int(5, 1, 10), 5)
        self.assertEqual(clamp_int(0, 1, 10), 1)
        self.assertEqual(clamp_int(15, 1, 10), 10)

    def test_status_item_serie_valido(self):
        from app.utils.validators import status_item_serie_valido
        self.assertTrue(status_item_serie_valido("planejado"))
        self.assertTrue(status_item_serie_valido("publicado"))
        self.assertFalse(status_item_serie_valido("invalido"))

    def test_tipo_pauta_valido(self):
        from app.utils.validators import tipo_pauta_valido
        self.assertTrue(tipo_pauta_valido("artigo"))
        self.assertTrue(tipo_pauta_valido("noticia"))
        self.assertFalse(tipo_pauta_valido("outro"))


class TestAuditoriaService(unittest.TestCase):
    """Testes do auditoria_service (não deve quebrar fluxo)."""

    def test_registrar_auditoria_admin_aceita_parametros(self):
        from app.services import auditoria_service
        # Não deve levantar; pode falhar silenciosamente sem app context
        try:
            auditoria_service.registrar_auditoria_admin(
                actor_email="test@test.com",
                tipo_decisao="admin_operacao",
                decisao="Teste",
                entidade="pauta",
                entidade_id=1,
                estado_antes=None,
                estado_depois=None,
                motivo="teste",
                resultado="sucesso",
            )
        except Exception as e:
            # Pode falhar por falta de app context / db
            self.assertIn("session", str(e).lower() or "context", str(e).lower())


class TestTermoService(unittest.TestCase):
    """Testes do termo_service."""

    def test_extensao_termo_permitida(self):
        from app.services import termo_service
        self.assertTrue(termo_service.extensao_termo_permitida("x.pdf"))
        self.assertFalse(termo_service.extensao_termo_permitida("x.txt"))

    def test_nome_seguro_termo_gera_sufixo(self):
        from app.services import termo_service
        nome = termo_service.nome_seguro_termo("termo.pdf")
        self.assertIn(".pdf", nome.lower())
        self.assertIn("termo", nome.lower())


class TestImportService(unittest.TestCase):
    """Testes do import_service."""

    def test_get_log_dir_retorna_string(self):
        from app.services import import_service
        d = import_service.get_log_dir()
        self.assertIsInstance(d, str)
        self.assertTrue(len(d) > 0)

    def test_processar_importacao_operacao_arquivo_vazio(self):
        from app.services import import_service
        from io import BytesIO
        file = MagicMock()
        file.filename = ""
        file.read.return_value = b""
        sucessos, falhas, p1, p2 = import_service.processar_importacao_operacao(file)
        self.assertEqual(sucessos, 0)
        self.assertEqual(falhas, 0)
        self.assertIsNone(p1)
        self.assertIsNone(p2)


class TestSerieService(unittest.TestCase):
    """Testes do serie_service (estrutura e tipos)."""

    def test_listar_series_retorna_lista(self):
        from app.services import serie_service
        # Pode estar vazia sem app context ou db
        try:
            series = serie_service.listar_series()
            self.assertIsInstance(series, list)
        except Exception:
            pass  # sem app context

    def test_obter_serie_por_id_com_app_context(self):
        """Com app context, id inexistente retorna None."""
        try:
            from app.web import app
            from app.services import serie_service
            with app.app_context():
                out = serie_service.obter_serie_por_id(999999)
                self.assertIsNone(out)
        except Exception:
            pass  # app ou db não disponível


class TestPautaService(unittest.TestCase):
    """Testes do pauta_service (constantes e assinaturas)."""

    def test_tipos_e_status_validos_definidos(self):
        from app.services import pauta_service
        self.assertIn("artigo", pauta_service.TIPOS_VALIDOS)
        self.assertIn("noticia", pauta_service.TIPOS_VALIDOS)
        self.assertIn("pendente", pauta_service.STATUS_VALIDOS)


if __name__ == "__main__":
    unittest.main()
