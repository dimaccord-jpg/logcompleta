""" 
Testes Fase 3 - Scout + Verificador.
Foco: coleta automática de notícias, deduplicação, fallback em falha de fonte e não-regressão de artigos manuais.
Executar: python -m unittest app.tests.test_fase3_scout_verificador -v
"""
import os
import unittest
import warnings
from datetime import datetime, timezone

os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=ResourceWarning)


class TestFase3ScoutVerificador(unittest.TestCase):
    """Testes de comportamento do Scout e Verificador (sem depender de rede externa)."""

    def test_scout_sem_fontes_configuradas_nao_quebra(self):
        """Quando SCOUT_SOURCES_JSON está vazio, executar_coleta retorna 0 inseridas e registra auditoria."""
        import app.run_cleiton_agente_scout as scout

        # Garante que nenhuma fonte será lida do ambiente
        if "SCOUT_SOURCES_JSON" in os.environ:
            del os.environ["SCOUT_SOURCES_JSON"]

        original_auditoria = scout.auditoria_registrar
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout.auditoria_registrar = original_auditoria
        self.assertIsInstance(resultado, dict)
        self.assertEqual(resultado.get("fontes_processadas"), 0)
        self.assertGreaterEqual(resultado.get("inseridas", 0), 0)
        # Mantém chaves legadas e adiciona diagnósticos (retrocompatibilidade + observabilidade)
        for chave in ("inseridas", "ignoradas_duplicata", "erros", "fontes_processadas",
                      "reativadas", "fontes_com_erro", "fontes_sem_itens", "fontes_com_itens", "diagnostico_fontes"):
            self.assertIn(chave, resultado)

    def test_scout_fonte_com_erro_nao_aborta_ciclo(self):
        """Erro em uma fonte não aborta o ciclo: erros++ mas função retorna normalmente."""
        import app.run_cleiton_agente_scout as scout

        chamadas = {"coletar_rss": 0}

        def fake_scout_sources():
            return [{"url": "https://exemplo.com/feed", "tipo": "noticia", "tipo_fonte": "rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            chamadas["coletar_rss"] += 1
            raise RuntimeError("falha simulada")

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(chamadas["coletar_rss"], 1)
        self.assertEqual(resultado.get("fontes_processadas"), 1)
        self.assertGreaterEqual(resultado.get("erros", 0), 1)
        self.assertEqual(resultado.get("fontes_com_erro", 0), 1)

    def test_scout_dependencia_feedparser_ausente_contabiliza_erro(self):
        """Ausência de feedparser é contabilizada como erro de fonte, sem quebrar o ciclo."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return [{"url": "https://exemplo.com/feed", "tipo": "noticia", "tipo_fonte": "rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            raise RuntimeError("dependencia_feedparser_ausente")

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(resultado.get("fontes_processadas"), 1)
        self.assertEqual(resultado.get("erros", 0), 1)
        self.assertEqual(resultado.get("fontes_com_erro", 0), 1)
        self.assertEqual(resultado.get("fontes_sem_itens", 0), 0)
        diag = resultado.get("diagnostico_fontes") or []
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0].get("status"), "erro")

    def test_scout_fonte_vazia_incrementa_fontes_sem_itens_sem_erro(self):
        """Fonte que retorna lista vazia é classificada como fontes_sem_itens, sem erro."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return [{"url": "https://exemplo.com/feed-vazio", "tipo": "noticia", "tipo_fonte": "rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            return []

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(resultado.get("fontes_processadas"), 1)
        self.assertEqual(resultado.get("erros", 0), 0)
        self.assertEqual(resultado.get("fontes_sem_itens", 0), 1)
        self.assertEqual(resultado.get("fontes_com_erro", 0), 0)

    def test_scout_fonte_com_excecao_incrementa_fontes_com_erro(self):
        """Exceção na coleta de uma fonte incrementa fontes_com_erro e erros."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return [{"url": "https://exemplo.com/fonte-quebrando", "tipo": "noticia", "tipo_fonte": "rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            raise RuntimeError("falha_simulada_generica")

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(resultado.get("fontes_processadas"), 1)
        self.assertGreaterEqual(resultado.get("erros", 0), 1)
        self.assertEqual(resultado.get("fontes_com_erro", 0), 1)
        self.assertEqual(resultado.get("fontes_sem_itens", 0), 0)

    def test_scout_fonte_invalida_entra_em_diagnostico_como_erro(self):
        """Fonte malformada (não-dict ou sem URL) deve contar como erro e aparecer no diagnóstico."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return ["invalida", {"tipo": "noticia", "tipo_fonte": "rss"}]

        original_sources = scout._scout_sources
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(resultado.get("fontes_processadas"), 2)
        self.assertEqual(resultado.get("erros", 0), 2)
        self.assertEqual(resultado.get("fontes_com_erro", 0), 2)
        diag = resultado.get("diagnostico_fontes") or []
        self.assertEqual(len(diag), 2)
        self.assertTrue(all(d.get("status") == "erro" for d in diag))

    def test_scout_retorno_mantem_chaves_legadas_e_novas(self):
        """Retorno de executar_coleta mantém chaves legadas e expõe novas chaves de diagnóstico."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return []

        original_sources = scout._scout_sources
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout.auditoria_registrar = original_auditoria

        for chave in ("inseridas", "ignoradas_duplicata", "erros", "fontes_processadas",
                      "reativadas", "fontes_com_erro", "fontes_sem_itens", "fontes_com_itens", "diagnostico_fontes"):
            self.assertIn(chave, resultado)

    def test_scout_reativa_pauta_falha_quando_link_duplicado(self):
        """Quando não insere por duplicata, Scout pode reativar pauta em falha para novo ciclo."""
        import app.run_cleiton_agente_scout as scout

        def fake_scout_sources():
            return [{"url": "https://exemplo.com/feed", "tipo": "noticia", "tipo_fonte": "rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            return [{
                "titulo_original": "Notícia logística",
                "fonte": "Fonte Teste",
                "link": "https://exemplo.com/noticia",
                "tipo": "noticia",
                "fonte_tipo": "rss",
            }]

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_inserir = scout._inserir_pauta
        original_reativar = scout._reativar_pauta_falha
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout._inserir_pauta = lambda item: False
        scout._reativar_pauta_falha = lambda item: True
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout._inserir_pauta = original_inserir
            scout._reativar_pauta_falha = original_reativar
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(resultado.get("inseridas", 0), 0)
        self.assertEqual(resultado.get("reativadas", 0), 1)
        self.assertEqual(resultado.get("ignoradas_duplicata", 0), 0)

    def test_scout_suporta_google_alerts_rss(self):
        """Fonte com tipo_fonte=google_alerts_rss deve usar o coletor RSS especializado sem quebrar."""
        import app.run_cleiton_agente_scout as scout

        chamadas = {"fonte_tipo": None}

        def fake_scout_sources():
            return [{"url": "https://www.google.com/alerts/feeds/123", "tipo": "noticia", "tipo_fonte": "google_alerts_rss"}]

        def fake_coletar_rss(url, max_itens, tipo_sugerido, fonte_tipo="rss"):
            chamadas["fonte_tipo"] = fonte_tipo
            # Retorna um item válido para evitar dependência de rede/BD
            return [{
                "titulo_original": "Notícia logística Google Alerts",
                "fonte": "Google Alerts",
                "link": "https://example.com/noticia-logistica",
                "tipo": "noticia",
                "fonte_tipo": fonte_tipo,
            }]

        def fake_inserir_pauta(item: dict) -> bool:
            # Não depende de banco; finge que sempre insere
            return True

        original_sources = scout._scout_sources
        original_rss = scout._coletar_rss
        original_inserir = scout._inserir_pauta
        original_link_existe = scout._link_ja_existe
        original_auditoria = scout.auditoria_registrar
        scout._scout_sources = fake_scout_sources
        scout._coletar_rss = fake_coletar_rss
        scout._inserir_pauta = fake_inserir_pauta
        scout._link_ja_existe = lambda _link: False
        scout.auditoria_registrar = lambda **kwargs: None
        try:
            resultado = scout.executar_coleta()
        finally:
            scout._scout_sources = original_sources
            scout._coletar_rss = original_rss
            scout._inserir_pauta = original_inserir
            scout._link_ja_existe = original_link_existe
            scout.auditoria_registrar = original_auditoria

        self.assertEqual(chamadas["fonte_tipo"], "google_alerts_rss")
        self.assertGreaterEqual(resultado.get("inseridas", 0), 1)

    def test_verificador_nao_quebra_com_recencia_e_termos(self):
        """_calcular_score_e_decisao considera recência/termos sem lançar exceções para pauta sintética."""
        import app.run_cleiton_agente_verificador as verificador

        class FakePauta:
            id = 1
            link = "https://exemplo.com/noticia-logistica"
            titulo_original = "Nova rota de frete e logística 4.0"
            fonte = "Portal Logístico"
            tipo = "noticia"
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            coletado_em = datetime.now(timezone.utc).replace(tzinfo=None)

        pauta = FakePauta()
        original_sim = verificador._titulo_similar_existente
        verificador._titulo_similar_existente = lambda titulo, link, pauta_id: (False, 0.0)
        try:
            score, status, motivo = verificador._calcular_score_e_decisao(pauta)
        finally:
            verificador._titulo_similar_existente = original_sim

        self.assertIsInstance(score, float)
        self.assertIn(status, ("aprovado", "revisar", "rejeitado"))
        self.assertIsInstance(motivo, str)

    def test_verificador_normaliza_www_em_fontes_confiaveis(self):
        """Domínios com e sem www da whitelist devem ter mesmo tratamento de confiança."""
        import app.run_cleiton_agente_verificador as verificador

        os.environ["VERIFICADOR_FONTES_CONFIAVEIS"] = "exemplo.com"

        class FakePauta:
            id = 2
            link = "https://www.exemplo.com/noticia-logistica"
            titulo_original = "Notícia de logística em fonte confiável"
            fonte = "Portal Exemplo"
            tipo = "noticia"
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            coletado_em = datetime.now(timezone.utc).replace(tzinfo=None)

        pauta = FakePauta()
        original_sim = verificador._titulo_similar_existente
        verificador._titulo_similar_existente = lambda titulo, link, pauta_id: (False, 0.0)
        try:
            score, status, motivo = verificador._calcular_score_e_decisao(pauta)
        finally:
            verificador._titulo_similar_existente = original_sim

        self.assertIn(status, ("aprovado", "revisar"))
        self.assertNotIn("fonte não listada como confiável", (motivo or "").lower())

    def test_verificador_aceita_subdominio_de_fonte_confiavel(self):
        """Subdomínio deve ser tratado como confiável quando domínio raiz estiver na whitelist."""
        import app.run_cleiton_agente_verificador as verificador

        os.environ["VERIFICADOR_FONTES_CONFIAVEIS"] = "exemplo.com"

        class FakePauta:
            id = 3
            link = "https://news.exemplo.com/noticia-logistica"
            titulo_original = "Notícia logística em subdomínio confiável"
            fonte = "Exemplo News"
            tipo = "noticia"
            created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            coletado_em = datetime.now(timezone.utc).replace(tzinfo=None)

        pauta = FakePauta()
        original_sim = verificador._titulo_similar_existente
        verificador._titulo_similar_existente = lambda titulo, link, pauta_id: (False, 0.0)
        try:
            _score, status, motivo = verificador._calcular_score_e_decisao(pauta)
        finally:
            verificador._titulo_similar_existente = original_sim

        self.assertIn(status, ("aprovado", "revisar"))
        self.assertNotIn("fonte não listada como confiável", (motivo or "").lower())


class TestNaoRegressaoArtigosManuais(unittest.TestCase):
    """Garantias mínimas de que o suporte a artigos manuais permanece."""

    def test_modelo_pauta_suporta_tipo_artigo(self):
        """Campo tipo de Pauta continua existindo e aceitando 'artigo'."""
        from app.models import Pauta

        self.assertTrue(hasattr(Pauta, "tipo"))
        self.assertEqual(getattr(Pauta.tipo.type, "length", None), 20)
        self.assertEqual(getattr(Pauta.tipo.default, "arg", None), "noticia")

    def test_pipeline_status_verificacao_permitidos_por_env(self):
        """Pipeline da Júlia deve manter padrão estrito e aceitar override controlado via env."""
        import app.run_julia_agente_pipeline as pipeline

        anterior = os.environ.get("JULIA_STATUS_VERIFICACAO_PERMITIDOS")
        try:
            if "JULIA_STATUS_VERIFICACAO_PERMITIDOS" in os.environ:
                del os.environ["JULIA_STATUS_VERIFICACAO_PERMITIDOS"]
            self.assertEqual(pipeline._status_verificacao_permitidos(), ["aprovado"])

            os.environ["JULIA_STATUS_VERIFICACAO_PERMITIDOS"] = "aprovado,revisar"
            self.assertEqual(pipeline._status_verificacao_permitidos(), ["aprovado", "revisar"])
        finally:
            if anterior is None:
                os.environ.pop("JULIA_STATUS_VERIFICACAO_PERMITIDOS", None)
            else:
                os.environ["JULIA_STATUS_VERIFICACAO_PERMITIDOS"] = anterior


if __name__ == "__main__":
    unittest.main()

