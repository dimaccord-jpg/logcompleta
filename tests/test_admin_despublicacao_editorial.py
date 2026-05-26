import os

from flask_login import UserMixin

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import AuditoriaGerencial, NoticiaPortal
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


class _AuthUser(UserMixin):
    def __init__(self, user_id: str):
        self.id = user_id


def _build_admin_client(app):
    app.config["SECRET_KEY"] = "test-secret-admin-despub"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret-admin-despub")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    from app.painel_admin.admin_routes import admin_bp

    if "admin" not in app.blueprints:
        app.register_blueprint(admin_bp)
    if "login" not in app.view_functions:
        app.add_url_rule("/login", "login", lambda: "login")
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _login(client, user_id: int) -> _AuthUser:
    user = _AuthUser(str(user_id))
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    return user


def test_admin_despublica_conteudo_com_auditoria(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-despub-admin")
        admin = seed_usuario(
            franquia.id,
            conta.id,
            email="admin-despub@test.com",
            categoria="free",
        )
        admin.is_admin = True
        db.session.add(admin)
        noticia = NoticiaPortal(
            tipo="artigo",
            titulo_julia="Artigo publicado",
            titulo_original="Original",
            link="https://example.com/artigo-publicado",
            fonte="Fonte",
            status_publicacao="publicado",
        )
        from app.models import utcnow_naive

        noticia.publicado_em = utcnow_naive()
        db.session.add(noticia)
        db.session.commit()

        client = _build_admin_client(app)
        _login(client, admin.id)
        resp = client.post(
            f"/admin/noticias/{noticia.id}/despublicar",
            data={"motivo": "conteudo_inadequado"},
        )
        assert resp.status_code == 302

        noticia_db = db.session.get(NoticiaPortal, noticia.id)
        assert noticia_db is not None
        assert noticia_db.publicado_em is None
        assert noticia_db.status_publicacao == "despublicado"

        auditoria = (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="admin_operacao",
                decisao="despublicacao_editorial",
            )
            .order_by(AuditoriaGerencial.id.desc())
            .first()
        )
        assert auditoria is not None
        assert auditoria.resultado == "sucesso"
        assert '"entidade": "noticia_portal"' in (auditoria.contexto_json or "")
        assert '"motivo": "conteudo_inadequado"' in (auditoria.contexto_json or "")


def test_nao_admin_nao_consegue_despublicar(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-despub-noadmin")
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="user-noadmin@test.com",
            categoria="free",
        )
        noticia = NoticiaPortal(
            tipo="noticia",
            titulo_julia="Notícia publicada",
            titulo_original="Original",
            link="https://example.com/noticia-publicada",
            fonte="Fonte",
            status_publicacao="publicado",
        )
        from app.models import utcnow_naive

        noticia.publicado_em = utcnow_naive()
        db.session.add(noticia)
        db.session.commit()

        client = _build_admin_client(app)
        _login(client, user.id)
        resp = client.post(
            f"/admin/noticias/{noticia.id}/despublicar",
            data={"motivo": "tentativa_sem_permissao"},
        )
        assert resp.status_code == 403

        noticia_db = db.session.get(NoticiaPortal, noticia.id)
        assert noticia_db is not None
        assert noticia_db.publicado_em is not None
        assert noticia_db.status_publicacao == "publicado"


def test_despublicado_sai_dos_filtros_publicos(app):
    with app.app_context():
        from app.models import utcnow_naive

        n_publicada = NoticiaPortal(
            tipo="artigo",
            titulo_julia="Publicada",
            titulo_original="Original publicada",
            link="https://example.com/publicada",
            fonte="Fonte",
            status_publicacao="publicado",
            publicado_em=utcnow_naive(),
        )
        n_despublicada = NoticiaPortal(
            tipo="artigo",
            titulo_julia="Despublicada",
            titulo_original="Original despublicada",
            link="https://example.com/despublicada",
            fonte="Fonte",
            status_publicacao="despublicado",
            publicado_em=None,
        )
        db.session.add(n_publicada)
        db.session.add(n_despublicada)
        db.session.commit()

        visiveis = (
            NoticiaPortal.query.filter(
                NoticiaPortal.tipo == "artigo",
                NoticiaPortal.publicado_em.isnot(None),
                NoticiaPortal.status_publicacao.in_(["publicado", "parcial"]),
            )
            .order_by(NoticiaPortal.data_publicacao.desc())
            .all()
        )
        assert [n.id for n in visiveis] == [n_publicada.id]
