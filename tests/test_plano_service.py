from app.extensions import db
from app.models import ConfigRegras, User
from app.services import plano_service
from tests.conftest import seed_conta_franquia_cliente


def test_listar_planos_saas_admin_nao_duplica_franquia_por_multiplos_usuarios(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-plano-distinct")
        db.session.add_all(
            [
                User(
                    email="starter-1@test.com",
                    full_name="Starter 1",
                    categoria="starter",
                    conta_id=conta.id,
                    franquia_id=franquia.id,
                ),
                User(
                    email="starter-2@test.com",
                    full_name="Starter 2",
                    categoria="starter",
                    conta_id=conta.id,
                    franquia_id=franquia.id,
                ),
            ]
        )
        db.session.commit()

        starter = next(
            p for p in plano_service.listar_planos_saas_admin() if p["codigo"] == "starter"
        )
        assert starter["franquias_vinculadas"] == 1


def test_corrigir_franquias_free_sem_limite_mantem_regressao_distinct_controlada(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-distinct")
        _, franquia_ref = seed_conta_franquia_cliente(slug="conta-free-ref")
        franquia.limite_total = None
        franquia_ref.limite_total = 42
        db.session.add_all([franquia, franquia_ref])
        db.session.add_all(
            [
                User(
                    email="free-1@test.com",
                    full_name="Free 1",
                    categoria="free",
                    conta_id=conta.id,
                    franquia_id=franquia.id,
                ),
                User(
                    email="free-2@test.com",
                    full_name="Free 2",
                    categoria="free",
                    conta_id=conta.id,
                    franquia_id=franquia.id,
                ),
            ]
        )
        db.session.add(
            ConfigRegras(
                chave="plano_franquia_ref_admin_free",
                descricao="ref free",
                valor_inteiro=franquia_ref.id,
                valor_texto=str(franquia_ref.id),
            )
        )
        db.session.commit()

        out = plano_service.corrigir_franquias_free_sem_limite()
        assert out["franquias_free_sem_limite_encontradas"] == 1
        assert out["franquias_free_atualizadas"] == 1
