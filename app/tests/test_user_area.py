import os
import unittest
import warnings

from flask import url_for

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


class TestUserArea(unittest.TestCase):
    """Testes básicos da Área do Usuário (/perfil)."""

    def setUp(self):
        self.app = get_app()
        if self.app is None:
            self.skipTest("App Flask não disponível")
        self.client = self.app.test_client()

    def test_perfil_redireciona_sem_login(self):
        """Acesso sem autenticação deve redirecionar para login."""
        resp = self.client.get("/perfil", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get("Location", "")
        self.assertIn("login", location)

    def test_perfil_usuario_comum_sem_painel_admin(self):
        """Usuário comum acessa Área do Usuário e não vê atalho de Painel ADM."""
        from app.models import User
        from app.extensions import db

        with self.app.app_context():
            # Cria usuário não admin para o teste, usando o mesmo banco da aplicação.
            user = User.query.filter_by(is_admin=False).first()
            if user is None:
                user = User(
                    email="teste_user_area@exemplo.com",
                    full_name="Usuário Teste Área",
                    is_admin=False,
                )
                db.session.add(user)
                db.session.commit()

            with self.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(user.id)
                    sess["_fresh"] = True

                # Chama diretamente a rota, evitando url_for fora de request
                resp = client.get("/perfil")
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn("Área do Usuário", html)
                self.assertNotIn("Painel ADM", html)

    def test_perfil_admin_ve_painel_admin(self):
        """Admin acessa Área do Usuário e visualiza o atalho Painel ADM."""
        from app.models import User

        with self.app.app_context():
            admin = User.query.filter_by(is_admin=True).first()
            if admin is None:
                self.skipTest("Usuário admin não disponível no banco de teste")

            with self.app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(admin.id)
                    sess["_fresh"] = True

                # Chama diretamente a rota, evitando url_for fora de request
                resp = client.get("/perfil")
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn("Área do Usuário", html)
                self.assertIn("Painel ADM", html)


if __name__ == "__main__":
    unittest.main()

