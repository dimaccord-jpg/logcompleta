"""
Serviço de gestão de planos e limites freemium.
Lê e persiste ConfigRegras (histórico chat Júlia, consultas/dia, trial) sem hardcode.
"""
import logging
from app.extensions import db
from app.models import ConfigRegras
from app.infra import (
    get_julia_chat_max_history,
    get_freemium_consultas_dia,
    get_freemium_trial_dias,
    CHAVE_JULIA_CHAT_MAX_HISTORY,
    CHAVE_FREEMIUM_CONSULTAS_DIA,
    CHAVE_FREEMIUM_TRIAL_DIAS,
)
from app.utils.validators import (
    JULIA_CHAT_MAX_HISTORY_MIN,
    JULIA_CHAT_MAX_HISTORY_MAX,
    FREEMIUM_CONSULTAS_DIA_MIN,
    FREEMIUM_CONSULTAS_DIA_MAX,
    FREEMIUM_TRIAL_DIAS_MIN,
    FREEMIUM_TRIAL_DIAS_MAX,
    parse_int_bounded,
)

logger = logging.getLogger(__name__)

DESCRICAO_JULIA_CHAT = "Limite histórico chat Júlia (freemium)"
DESCRICAO_CONSULTAS_DIA = "Consultas grátis por dia (chat Júlia)"
DESCRICAO_TRIAL_DIAS = "Dias de trial (999999999 = ilimitado)"


def obter_config_planos():
    """
    Retorna dict com configuração atual de planos para exibição no admin:
    plano_ativo, indice_reajuste (placeholder), limites freemium lidos do infra/ConfigRegras.
    """
    return {
        "plano_ativo": "Premium",
        "indice_reajuste": 1.05,
        "julia_chat_max_history": get_julia_chat_max_history(),
        "freemium_consultas_dia": get_freemium_consultas_dia(),
        "freemium_trial_dias": get_freemium_trial_dias(),
    }


def salvar_limite_freemium(
    julia_chat_max_history_raw: str | None = None,
    freemium_consultas_dia_raw: str | None = None,
    freemium_trial_dias_raw: str | None = None,
) -> list[str]:
    """
    Persiste em ConfigRegras os limites freemium enviados pelo form.
    Retorna lista de mensagens descritivas do que foi salvo (ex.: ["histórico chat = 10"]).
    """
    msgs = []
    try:
        if julia_chat_max_history_raw:
            v = parse_int_bounded(
                julia_chat_max_history_raw,
                JULIA_CHAT_MAX_HISTORY_MIN,
                JULIA_CHAT_MAX_HISTORY_MAX,
            )
            if v is not None:
                cfg = ConfigRegras.query.filter_by(
                    chave=CHAVE_JULIA_CHAT_MAX_HISTORY
                ).first()
                if not cfg:
                    cfg = ConfigRegras(
                        chave=CHAVE_JULIA_CHAT_MAX_HISTORY,
                        descricao=DESCRICAO_JULIA_CHAT,
                    )
                    db.session.add(cfg)
                cfg.valor_inteiro = v
                cfg.valor_texto = None
                msgs.append(f"histórico chat = {v}")

        if freemium_consultas_dia_raw:
            v = parse_int_bounded(
                freemium_consultas_dia_raw,
                FREEMIUM_CONSULTAS_DIA_MIN,
                FREEMIUM_CONSULTAS_DIA_MAX,
            )
            if v is not None:
                cfg = ConfigRegras.query.filter_by(
                    chave=CHAVE_FREEMIUM_CONSULTAS_DIA
                ).first()
                if not cfg:
                    cfg = ConfigRegras(
                        chave=CHAVE_FREEMIUM_CONSULTAS_DIA,
                        descricao=DESCRICAO_CONSULTAS_DIA,
                    )
                    db.session.add(cfg)
                cfg.valor_inteiro = v
                cfg.valor_texto = None
                msgs.append(f"consultas/dia = {v}")

        if freemium_trial_dias_raw:
            v = parse_int_bounded(
                freemium_trial_dias_raw,
                FREEMIUM_TRIAL_DIAS_MIN,
                FREEMIUM_TRIAL_DIAS_MAX,
            )
            if v is not None:
                cfg = ConfigRegras.query.filter_by(
                    chave=CHAVE_FREEMIUM_TRIAL_DIAS
                ).first()
                if not cfg:
                    cfg = ConfigRegras(
                        chave=CHAVE_FREEMIUM_TRIAL_DIAS,
                        descricao=DESCRICAO_TRIAL_DIAS,
                    )
                    db.session.add(cfg)
                cfg.valor_inteiro = v
                cfg.valor_texto = None
                msgs.append(
                    f"trial = {v} dias" if v < 999999999 else "trial = ilimitado"
                )

        if msgs:
            db.session.commit()
    except Exception as e:
        logger.exception("Erro ao salvar freemium: %s", e)
        db.session.rollback()
        raise
    return msgs
