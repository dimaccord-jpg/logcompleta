"""
Testes de robustez da redação da Júlia.
Garante fallback local quando o provedor de IA falha.
"""
import unittest
from unittest.mock import patch


class TestJuliaRedacaoFallback(unittest.TestCase):

    def test_fallback_artigo_sem_cliente(self):
        from app.run_julia_agente_redacao import gerar_artigo_completo

        with patch("app.run_julia_agente_redacao._client_for_tipo", return_value=None):
            data = gerar_artigo_completo("Teste artigo", "Fonte X", "https://example.com/a")

        self.assertIsInstance(data, dict)
        self.assertTrue(data.get("conteudo_completo"))
        self.assertEqual(data.get("objetivo_lead"), "contato_comercial")
        self.assertTrue(data.get("cta"))

    def test_fallback_noticia_sem_cliente(self):
        from app.run_julia_agente_redacao import gerar_noticia_curta

        with patch("app.run_julia_agente_redacao._client_for_tipo", return_value=None):
            data = gerar_noticia_curta("Teste notícia", "Fonte Y", "https://example.com/n")

        self.assertIsInstance(data, dict)
        self.assertTrue(data.get("titulo_julia"))
        self.assertTrue(data.get("resumo_julia"))
        self.assertTrue(data.get("prompt_imagem"))


if __name__ == "__main__":
    unittest.main()
