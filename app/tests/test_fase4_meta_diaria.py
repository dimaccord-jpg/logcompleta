"""
Suite de testes Fase 4 - Meta diária de artigo, fallback em cadeia e não regressão de notícias rápidas.
Executar:
- python -m unittest app.tests.test_fase4_meta_diaria -v
ou
- pytest app/tests/test_fase4_meta_diaria.py -v
"""
import os
import unittest
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "dev")


class DummyApp:
    """App mínimo para executar o ciclo gerencial sem depender do Flask real."""

    def app_context(self):
        from contextlib import nullcontext

        return nullcontext()


class TestFase4MetaDiariaEFallback(unittest.TestCase):
    """Testes de meta diária de artigo e fallback em cadeia no orquestrador."""

    def _ativar_patches(self, patches):
        stack = ExitStack()
        for p in patches:
            stack.enter_context(p)
        return stack

    def _patch_base(self):
        """Patches comuns para isolar o ciclo do Cleiton de dependências externas."""
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
            patch("app.run_cleiton_agente_scout.executar_coleta", return_value={"inseridas": 0, "ignoradas_duplicata": 0, "erros": 0, "fontes_processadas": 0}),
            patch("app.run_cleiton_agente_verificador.executar_verificacao", return_value={"processadas": 0, "aprovadas": 0, "revisar": 0, "rejeitadas": 0}),
            patch("app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria", return_value=None),
            patch("app.run_cleiton_agente_customer_insight.executar_insight"),
        ]
        return base_patches

    def test_artigo_ja_publicado_hoje_forca_noticia(self):
        """
        Quando já existe artigo publicado hoje, decidir_tipo_missao deve cair para 'noticia'.
        Regra baseada em publicação efetiva (NoticiaPortal), não apenas tentativas.
        """
        from app.run_cleiton_agente_orquestrador import decidir_tipo_missao

        fake_hoje = datetime(2025, 1, 1, 12, 0, 0)

        with patch("app.run_cleiton_agente_orquestrador._utcnow_naive", return_value=fake_hoje), \
                patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=True), \
            patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(None, None)), \
            patch("app.run_cleiton_agente_orquestrador.status_verificacao_permitidos", return_value=["aprovado"]), \
                patch("app.run_cleiton_agente_orquestrador.Pauta") as PautaMock:
            # Simula que há pelo menos uma pauta de artigo pendente (não deveria ser usada pois já há artigo hoje)
            PautaMock.query.filter.return_value.first.return_value = object()
            tipo = decidir_tipo_missao()
        self.assertEqual(tipo, "noticia")

    def test_limite_diario_tentativas_artigo_ignora_ciclo(self):
        """
        Quando tentativas de artigo atingem o limite diário, ciclo deve ser ignorado
        com caminho_usado='limite_artigo_dia' e sem dispatch.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        base = self._patch_base()
        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=3),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertIsInstance(resultado, dict)
        self.assertEqual(resultado.get("status"), "ignorado")
        self.assertEqual(resultado.get("caminho_usado"), "limite_artigo_dia")
        self.assertIn("Limite diário de tentativas de artigo atingido", resultado.get("motivo_final", ""))

    def test_execucao_manual_forcada_artigo_ignora_trava_diaria(self):
        """
        Execução manual forçada de artigo deve ignorar a trava diária de tentativas/artigo publicado hoje.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        base = self._patch_base()
        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=3),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=True),
            patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(None, None)),
            patch("app.run_cleiton_agente_orquestrador._buscar_pauta_manual_artigo", return_value=None),
            patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "m_forcada", "tipo_missao": "artigo"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(
                DummyApp(),
                bypass_frequencia=True,
                tipo_missao_forcado="artigo",
                ignorar_trava_artigo_hoje=True,
            )
        self.assertIsInstance(resultado, dict)
        # Mesmo com travas diárias simuladas, o caminho não deve ser limite_artigo_dia
        self.assertNotEqual(resultado.get("caminho_usado"), "limite_artigo_dia")

    def test_fallback_serie_dia_define_caminho(self):
        """
        Quando há item de série do dia com pauta preparada, caminho_usado deve refletir 'serie_dia'.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        class FakeItem:
            id = 1
            serie_id = 10

        base = self._patch_base()
        hoje = datetime(2025, 1, 1, 10, 0, 0)

        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=0),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
            patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(FakeItem(), "serie_dia")),
            patch("app.run_cleiton_agente_orquestrador.preparar_pauta_para_item", return_value=object()),
            patch("app.run_cleiton_agente_orquestrador._utcnow_naive", return_value=hoje),
            patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "m1", "tipo_missao": "artigo"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("tipo_missao"), "artigo")
        self.assertEqual(resultado.get("caminho_usado"), "serie_dia")

    def test_fallback_serie_atrasada_define_caminho(self):
        """
        Quando há item de série atrasada com pauta preparada, caminho_usado deve refletir 'serie_atrasada'.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        class FakeItem:
            id = 2
            serie_id = 20

        base = self._patch_base()
        hoje = datetime(2025, 1, 2, 10, 0, 0)

        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=0),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
            patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(FakeItem(), "serie_atrasada")),
            patch("app.run_cleiton_agente_orquestrador.preparar_pauta_para_item", return_value=object()),
            patch("app.run_cleiton_agente_orquestrador._utcnow_naive", return_value=hoje),
            patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "m2", "tipo_missao": "artigo"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("tipo_missao"), "artigo")
        self.assertEqual(resultado.get("caminho_usado"), "serie_atrasada")

    def test_falha_preparar_pauta_serie_faz_fallback_para_manual(self):
        """
        Quando preparar_pauta_para_item falha, orquestrador deve tentar pauta manual elegível
        e marcar caminho_usado='pauta_manual'.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        class FakeItem:
            id = 3
            serie_id = 30

        class FakePauta:
            id = 100

        base = self._patch_base()

        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=0),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
            patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(FakeItem(), "serie_dia")),
            patch("app.run_cleiton_agente_orquestrador.preparar_pauta_para_item", return_value=None),
            patch("app.run_cleiton_agente_orquestrador._buscar_pauta_manual_artigo", return_value=FakePauta()),
            patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "m3", "tipo_missao": "artigo"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("tipo_missao"), "artigo")
        self.assertEqual(resultado.get("caminho_usado"), "pauta_manual")

    def test_sem_fonte_artigo_encerra_sem_dispatch(self):
        """
        Quando não há item de série nem pauta manual elegível, ciclo deve ser ignorado,
        sem despachar missão (sem tentar executar Júlia).
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        base = self._patch_base()
        with ExitStack() as stack:
            for p in base:
                stack.enter_context(p)
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="artigo"))
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=0))
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3))
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False))
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador.selecionar_item_para_missao", return_value=(None, None)))
            stack.enter_context(patch("app.run_cleiton_agente_orquestrador._buscar_pauta_manual_artigo", return_value=None))
            construir_payload_mock = stack.enter_context(patch("app.run_cleiton_agente_orquestrador.construir_payload"))
            registrar_missao_mock = stack.enter_context(patch("app.run_cleiton_agente_orquestrador.registrar_missao"))
            despachar_mock = stack.enter_context(patch("app.run_cleiton_agente_orquestrador.despachar"))
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("status"), "ignorado")
        self.assertEqual(resultado.get("caminho_usado"), "sem_fonte_artigo")
        self.assertIn("Nenhum item de série ou pauta manual elegível para artigo", resultado.get("motivo_final", ""))
        construir_payload_mock.assert_not_called()
        registrar_missao_mock.assert_not_called()
        despachar_mock.assert_not_called()

    def test_caminho_usado_e_motivo_final_em_retorno_antecipado_frequencia_e_janela(self):
        """
        Retornos antecipados por frequência/janela devem preencher caminho_usado e motivo_final.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        # Frequência
        with patch("app.run_cleiton_agente_orquestrador.bootstrap_regras"), \
                patch("app.run_cleiton_agente_orquestrador.bootstrap_plano_se_necessario"), \
                patch("app.run_cleiton_agente_orquestrador.auditoria_registrar"), \
                patch("app.run_cleiton_agente_orquestrador.obter_plano_ativo", return_value=None), \
                patch("app.run_cleiton_agente_orquestrador.ultima_auditoria_orquestracao", return_value=None), \
                patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False), \
                patch("app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia", return_value=False):
            r_freq = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)
        self.assertEqual(r_freq.get("status"), "ignorado")
        self.assertEqual(r_freq.get("caminho_usado"), "ignorado_frequencia")
        self.assertTrue(r_freq.get("motivo_final"))

        # Janela
        with patch("app.run_cleiton_agente_orquestrador.bootstrap_regras"), \
                patch("app.run_cleiton_agente_orquestrador.bootstrap_plano_se_necessario"), \
                patch("app.run_cleiton_agente_orquestrador.auditoria_registrar"), \
                patch("app.run_cleiton_agente_orquestrador.obter_plano_ativo", return_value=None), \
                patch("app.run_cleiton_agente_orquestrador.ultima_auditoria_orquestracao", return_value=None), \
                patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False), \
                patch("app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia", return_value=True), \
                patch("app.run_cleiton_agente_orquestrador.dentro_janela_publicacao", return_value=False), \
                patch("app.run_cleiton_agente_orquestrador.get_janela_publicacao", return_value=(6, 22)):
            r_janela = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(r_janela.get("status"), "ignorado")
        self.assertEqual(r_janela.get("caminho_usado"), "fora_janela_publicacao")
        self.assertTrue(r_janela.get("motivo_final"))

    def test_fluxo_noticia_rapida_permanece_inalterado(self):
        """
        Quando decidir_tipo_missao retorna 'noticia', ciclo segue fluxo de notícias rápidas
        e caminho_usado='noticia_rapida' sem interferência da meta diária de artigo.
        """
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        base = self._patch_base()

        extra = [
            patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="noticia"),
            patch("app.run_cleiton_agente_orquestrador._tentativas_artigo_hoje", return_value=0),
            patch("app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia", return_value=3),
            patch("app.run_cleiton_agente_orquestrador._artigo_publicado_hoje", return_value=False),
            patch("app.run_cleiton_agente_scout.executar_coleta", return_value={"inseridas": 1, "ignoradas_duplicata": 0, "erros": 0, "fontes_processadas": 1}),
            patch("app.run_cleiton_agente_verificador.executar_verificacao", return_value={"processadas": 1, "aprovadas": 1, "revisar": 0, "rejeitadas": 0}),
            patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "mn1", "tipo_missao": "noticia"}),
            patch("app.run_cleiton_agente_orquestrador.registrar_missao"),
            patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False),
        ]
        with self._ativar_patches(base + extra):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)

        self.assertEqual(resultado.get("tipo_missao"), "noticia")
        self.assertEqual(resultado.get("caminho_usado"), "noticia_rapida")
        # Mantém chaves legadas importantes no retorno (contrato admin/cron)
        for chave in ("status", "motivo", "scout", "verificador"):
            self.assertIn(chave, resultado)


if __name__ == "__main__":
    unittest.main()

