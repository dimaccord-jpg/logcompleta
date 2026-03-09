"""
Testes para rota admin de bypass do Cleiton e retorno estruturado do orquestrador.
Executar: python -m unittest app.tests.test_admin_bypass_cleiton -v
"""
import os
import unittest
import warnings
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "dev")
warnings.filterwarnings("ignore", category=ResourceWarning)


def get_app():
    try:
        from app.env_loader import load_app_env
        load_app_env()
        from app.web import app
        return app
    except Exception:
        return None


class TestAdminBypassCleiton(unittest.TestCase):
    """Verifica feedback da rota admin de bypass do Cleiton."""

    def setUp(self):
        self.app = get_app()
        if self.app is None:
            self.skipTest("App Flask não disponível")
        self.client = self.app.test_client()

    def test_bypass_fora_janela_retorna_warning(self):
        """Quando orquestrador retorna status=ignorado (fora_janela), rota deve fazer flash warning (não sucesso)."""
        from app.models import User

        fake_resultado = {
            "status": "ignorado",
            "motivo": "Fora da janela de publicação; ciclo não executado.",
            "bypass_frequencia": True,
            "fora_janela": True,
            "ignorado_frequencia": False,
            "tipo_missao": None,
            "mission_id": None,
            "dispatch_ok": None,
            "scout": None,
            "verificador": None,
        }

        with self.app.app_context():
            admin = User.query.filter_by(is_admin=True).first()
            if admin is None:
                self.skipTest("Usuário admin não disponível no banco de teste")

            with self.app.test_client() as client, patch("app.run_cleiton.executar_orquestracao", return_value=fake_resultado):
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(admin.id)
                    sess["_fresh"] = True

                resp = client.post("/admin/agentes/julia/executar-cleiton", data={"bypass_frequencia": "1"}, follow_redirects=True)
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                # Espera que a mensagem de motivo apareça e que a classe de alerta corresponda a warning
                self.assertIn("Fora da janela de publicação; ciclo não executado.", html)
                self.assertIn("alert-warning", html)


class TestOrquestradorRetornoEstruturado(unittest.TestCase):
    """Testes mínimos do retorno estruturado do orquestrador."""

    def test_retornado_dict_ignorado_frequencia(self):
        """Quando pode_executar_por_frequencia retorna False, status deve ser ignorado e ignorado_frequencia=True."""
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        class DummyApp:
            def app_context(self):
                from contextlib import nullcontext
                return nullcontext()

        with patch("app.run_cleiton_agente_orquestrador.bootstrap_regras"), \
             patch("app.run_cleiton_agente_orquestrador.bootstrap_plano_se_necessario"), \
               patch("app.run_cleiton_agente_orquestrador.auditoria_registrar"), \
             patch("app.run_cleiton_agente_orquestrador.obter_plano_ativo", return_value=None), \
             patch("app.run_cleiton_agente_orquestrador.ultima_auditoria_orquestracao", return_value=None), \
             patch("app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia", return_value=False):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)
        self.assertIsInstance(resultado, dict)
        self.assertEqual(resultado.get("status"), "ignorado")
        self.assertTrue(resultado.get("ignorado_frequencia"))

    def test_retornado_dict_despacho_falha(self):
        """Quando despachar retorna False, status deve ser falha e dispatch_ok=False."""
        from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial

        class DummyApp:
            def app_context(self):
                from contextlib import nullcontext
                return nullcontext()

        with patch("app.run_cleiton_agente_orquestrador.bootstrap_regras"), \
             patch("app.run_cleiton_agente_orquestrador.bootstrap_plano_se_necessario"), \
               patch("app.run_cleiton_agente_orquestrador.auditoria_registrar"), \
             patch("app.run_cleiton_agente_orquestrador.obter_plano_ativo", return_value=None), \
             patch("app.run_cleiton_agente_orquestrador.ultima_auditoria_orquestracao", return_value=None), \
               patch("app.run_cleiton_agente_orquestrador.get_prioridade_padrao", return_value=5), \
             patch("app.run_cleiton_agente_orquestrador.get_janela_publicacao", return_value=(6, 22)), \
             patch("app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia", return_value=True), \
             patch("app.run_cleiton_agente_orquestrador.dentro_janela_publicacao", return_value=True), \
             patch("app.run_cleiton_agente_orquestrador.decidir_tipo_missao", return_value="noticia"), \
               patch("app.run_cleiton_agente_scout.executar_coleta", return_value={"inseridas": 0, "ignoradas_duplicata": 0, "erros": 0, "fontes_processadas": 0}), \
               patch("app.run_cleiton_agente_verificador.executar_verificacao", return_value={"processadas": 0, "aprovadas": 0, "revisar": 0, "rejeitadas": 0}), \
               patch("app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria", return_value=None), \
             patch("app.run_cleiton_agente_customer_insight.executar_insight"), \
             patch("app.run_cleiton_agente_orquestrador.construir_payload", return_value={"mission_id": "m1", "tipo_missao": "noticia"}), \
             patch("app.run_cleiton_agente_orquestrador.registrar_missao"), \
             patch("app.run_cleiton_agente_orquestrador.despachar", return_value=False), \
             patch("app.run_cleiton_agente_orquestrador.executar_retencao"):
            resultado = executar_ciclo_gerencial(DummyApp(), bypass_frequencia=False)
        self.assertIsInstance(resultado, dict)
        self.assertEqual(resultado.get("status"), "falha")
        self.assertFalse(resultado.get("dispatch_ok"))

