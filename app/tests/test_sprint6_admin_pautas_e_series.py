"""
Suite Sprint 6 - Operação OFF no admin (pautas e séries).
Executar:
- python -m unittest app.tests.test_sprint6_admin_pautas_e_series -v
"""
import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("APP_ENV", "dev")

_APP_CACHE = None


def get_app():
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


def _unique_link(sufixo: str) -> str:
    token = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"https://example.com/{sufixo}-{token}"


class TestAdminPautasCRUD(unittest.TestCase):
    """CRUD mínimo de pautas no admin (happy path + validações)."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app
        self.client = self.app.test_client()

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def _get_admin_user(self):
        from app.models import User

        with self.app.app_context():
            return User.query.filter_by(is_admin=True).first()

    def test_listar_pautas_requer_login_admin(self):
        """Rota /admin/pautas existe e exige admin logado."""
        resp = self.client.get("/admin/pautas")
        # login_required deve redirecionar
        self.assertIn(resp.status_code, (302, 401, 403))

        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp2 = c.get("/admin/pautas")
            self.assertNotEqual(resp2.status_code, 500)

    def test_criar_pauta_manual_happy_path(self):
        """Criar pauta manual via admin deve persistir com fonte_tipo manual."""
        from app.extensions import db
        from app.models import Pauta
        from app.infra import ensure_database_schema

        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")

        with self.app.app_context():
            ensure_database_schema(db)

        link = _unique_link("pauta-sprint6")

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                "/admin/pautas/salvar",
                data={
                    "titulo_original": "Pauta Sprint 6 Admin",
                    "link": link,
                    "fonte": "Admin",
                    "tipo": "artigo",
                    "status": "pendente",
                    "status_verificacao": "aprovado",
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Pauta salva com sucesso", html)

        with self.app.app_context():
            pauta = (
                Pauta.query.filter_by(link=link)
                .order_by(Pauta.id.desc())
                .first()
            )
            self.assertIsNotNone(pauta)
            self.assertEqual(pauta.tipo, "artigo")
            self.assertEqual(pauta.status, "pendente")
            # Fonte manual preserva compatibilidade com fluxo atual
            self.assertEqual(pauta.fonte_tipo, "manual")

    def test_validacao_campos_obrigatorios_pauta(self):
        """Título e link obrigatórios disparam warning e não quebram admin."""
        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                "/admin/pautas/salvar",
                data={
                    "titulo_original": "",
                    "link": "",
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Título e link são obrigatórios para a pauta", html)


class TestVinculoPautaItemSerie(unittest.TestCase):
    """Vínculo/desvínculo pauta-item de série com cenários válidos e inválidos."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app
        self.client = self.app.test_client()

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def _get_admin_user(self):
        from app.models import User

        with self.app.app_context():
            return User.query.filter_by(is_admin=True).first()

    def _criar_serie_item_e_pauta(self):
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial, Pauta

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Sprint 6",
                tema="Tema Sprint 6",
                cadencia_dias=1,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status="planejado",
                data_planejada=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.session.add(item)
            pauta = Pauta(
                titulo_original="Pauta vínculo Sprint 6",
                fonte="Admin",
                link=_unique_link("pauta-vinculo-s6"),
                tipo="artigo",
                status="pendente",
                status_verificacao="aprovado",
                fonte_tipo="manual",
            )
            db.session.add(pauta)
            db.session.commit()
            return serie.id, item.id, pauta.id

    def test_vincular_pauta_item_happy_path(self):
        """Vínculo pauta-item válido atualiza pauta_id e registra sucesso."""
        from app.extensions import db
        from app.models import SerieItemEditorial

        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        serie_id, item_id, pauta_id = self._criar_serie_item_e_pauta()

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                f"/admin/series/{serie_id}/itens/{item_id}/vincular-pauta",
                data={
                    "pauta_id": pauta_id,
                    "motivo": "Vínculo Sprint 6",
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Pauta vinculada ao item com sucesso", html)

        with self.app.app_context():
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertEqual(item.pauta_id, pauta_id)

    def test_vinculo_pauta_ja_usada_em_outro_item_bloqueado(self):
        """Pauta já vinculada a outro item não pode ser reutilizada."""
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial, Pauta

        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Sprint 6 Vínculo Inválido",
                tema="Tema",
                cadencia_dias=1,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            pauta = Pauta(
                titulo_original="Pauta duplicada",
                fonte="Admin",
                link=_unique_link("pauta-duplicada"),
                tipo="artigo",
                status="pendente",
                status_verificacao="aprovado",
                fonte_tipo="manual",
            )
            db.session.add(pauta)
            db.session.flush()
            item1 = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status="planejado",
            )
            db.session.add(item1)
            db.session.flush()
            item1.pauta_id = pauta.id
            item2 = SerieItemEditorial(
                serie_id=serie.id,
                ordem=2,
                status="planejado",
            )
            db.session.add(item2)
            db.session.commit()
            serie_id_val = serie.id
            item2_id = item2.id
            pauta_id_val = pauta.id

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                f"/admin/series/{serie_id_val}/itens/{item2_id}/vincular-pauta",
                data={
                    "pauta_id": pauta_id_val,
                    "motivo": "Tentativa de reutilização",
                },
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Pauta já está vinculada a outro item de série", html)


class TestAcoesAssistidasMotivoObrigatorio(unittest.TestCase):
    """Ações assistidas (reabrir/pular) exigem motivo e respeitam máquina de estados."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()
        if cls.app is None:
            raise unittest.SkipTest("App Flask não disponível")

    def setUp(self):
        self.app = self.__class__.app
        self.client = self.app.test_client()

    def tearDown(self):
        _cleanup_db_resources(self.app, dispose_engines=False)

    @classmethod
    def tearDownClass(cls):
        _cleanup_db_resources(getattr(cls, "app", None), dispose_engines=True)

    def _get_admin_user(self):
        from app.models import User

        with self.app.app_context():
            return User.query.filter_by(is_admin=True).first()

    def _criar_item(self, status: str):
        from app.extensions import db
        from app.infra import ensure_database_schema
        from app.models import SerieEditorial, SerieItemEditorial

        with self.app.app_context():
            ensure_database_schema(db)
            serie = SerieEditorial(
                nome="Série Ações Assistidas",
                tema="Tema",
                cadencia_dias=1,
                ativo=True,
            )
            db.session.add(serie)
            db.session.flush()
            item = SerieItemEditorial(
                serie_id=serie.id,
                ordem=1,
                status=status,
            )
            db.session.add(item)
            db.session.commit()
            return serie.id, item.id

    def test_reabrir_item_exige_motivo(self):
        """Reabrir item sem motivo é bloqueado com feedback amigável."""
        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        serie_id, item_id = self._criar_item(status="falha")

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                f"/admin/series/{serie_id}/itens/{item_id}/reabrir",
                data={"motivo": ""},
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Informe um motivo para reabrir o item", html)

    def test_reabrir_item_happy_path_altera_status(self):
        """Reabrir item com motivo válido altera status para planejado."""
        from app.extensions import db
        from app.models import SerieItemEditorial

        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        serie_id, item_id = self._criar_item(status="falha")

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                f"/admin/series/{serie_id}/itens/{item_id}/reabrir",
                data={"motivo": "Reabrir para novo teste"},
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Item reaberto como planejado", html)

        with self.app.app_context():
            item = db.session.get(SerieItemEditorial, item_id)
            self.assertEqual(item.status, "planejado")

    def test_pular_item_exige_motivo(self):
        """Pular item sem motivo é bloqueado com feedback amigável."""
        admin = self._get_admin_user()
        if admin is None:
            self.skipTest("Usuário admin não disponível no banco de teste")
        serie_id, item_id = self._criar_item(status="planejado")

        with self.client as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(admin.id)
                sess["_fresh"] = True
            resp = c.post(
                f"/admin/series/{serie_id}/itens/{item_id}/pular",
                data={"motivo": ""},
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Informe um motivo para pular o item", html)


if __name__ == "__main__":
    unittest.main()

