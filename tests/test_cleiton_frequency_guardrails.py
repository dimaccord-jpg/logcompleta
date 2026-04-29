from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import AuditoriaGerencial, ConfigRegras
from app.run_cleiton_agente_auditoria import registrar
from app.run_cleiton_agente_orquestrador import ultima_auditoria_orquestracao
from app.run_cleiton_agente_regras import (
    CHAVE_FREQUENCIA_HORAS,
    CHAVE_FREQUENCIA_MINUTOS,
    bootstrap_regras,
    configurar_frequencia_minutos,
    pode_executar_por_frequencia,
)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_auditoria_ignorado_recente_nao_renova_ultima_execucao(app):
    with app.app_context():
        bootstrap_regras()
        configurar_frequencia_minutos(5)

        sucesso = AuditoriaGerencial(
            tipo_decisao="orquestracao",
            decisao="Ciclo executado",
            resultado="sucesso",
            created_at=_utcnow_naive() - timedelta(minutes=10),
        )
        ignorado = AuditoriaGerencial(
            tipo_decisao="orquestracao",
            decisao="Ciclo ignorado por frequencia",
            resultado="ignorado",
            created_at=_utcnow_naive() - timedelta(minutes=1),
        )
        db.session.add_all([sucesso, ignorado])
        db.session.commit()

        ultima = ultima_auditoria_orquestracao()

        assert ultima == sucesso.created_at
        assert pode_executar_por_frequencia(ultima, agora=_utcnow_naive()) is True


def test_auditoria_sucesso_recente_bloqueia_corretamente(app):
    with app.app_context():
        bootstrap_regras()
        configurar_frequencia_minutos(5)

        ultima = _utcnow_naive() - timedelta(minutes=1)
        db.session.add(
            AuditoriaGerencial(
                tipo_decisao="orquestracao",
                decisao="Ciclo executado",
                resultado="sucesso",
                created_at=ultima,
            )
        )
        db.session.commit()

        ref = ultima_auditoria_orquestracao()
        assert ref == ultima
        assert pode_executar_por_frequencia(ref, agora=_utcnow_naive()) is False


def test_auditoria_bypass_continua_ignoradaa_na_referencia(app):
    with app.app_context():
        bootstrap_regras()
        configurar_frequencia_minutos(5)

        registrar(
            tipo_decisao="orquestracao",
            decisao="Bypass manual de frequencia aplicado",
            contexto={"bypass_frequencia": True},
            resultado="sucesso",
        )

        assert ultima_auditoria_orquestracao() is None


def test_configurar_frequencia_minutos_preserva_compatibilidade_legada(app):
    with app.app_context():
        bootstrap_regras()
        configurar_frequencia_minutos(30)

        cfg_min = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_MINUTOS).first()
        cfg_horas = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()

        assert cfg_min is not None
        assert cfg_min.valor_inteiro == 30
        assert cfg_horas is not None
        assert cfg_horas.valor_inteiro == 1
