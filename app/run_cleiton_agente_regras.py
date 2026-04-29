"""
Cleiton - Agente Regras: engine de regras (prioridade, frequência, janela, retry).
Regras vêm de configuração persistida (ConfigRegras); sem hardcode de valores de negócio.
"""
import logging
import math
from datetime import datetime, timedelta, timezone
from app.extensions import db
from app.models import ConfigRegras

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para comparação consistente com timestamps salvos no banco."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Chaves de configuração (valores padrão só quando DB não tem registro)
CHAVE_FREQUENCIA_HORAS = "frequencia_horas"
CHAVE_FREQUENCIA_MINUTOS = "frequencia_minutos"
CHAVE_PRIORIDADE_PADRAO = "prioridade_padrao"
CHAVE_JANELA_INICIO = "janela_publicacao_inicio"  # hora 0-23
CHAVE_JANELA_FIM = "janela_publicacao_fim"
CHAVE_MAX_RETRIES = "max_retries"
CHAVE_RETENCAO_MESES_DADOS = "retencao_meses_dados"
CHAVE_RETENCAO_MESES_IMAGENS = "retencao_meses_imagens"
# Limite de tentativas de artigo por dia (meta diária) - Sprint 4
CHAVE_MAX_TENTATIVAS_ARTIGO_DIA = "max_tentativas_artigo_dia"

DEFAULTS = {
    CHAVE_FREQUENCIA_HORAS: 3,
    CHAVE_FREQUENCIA_MINUTOS: 180,
    CHAVE_PRIORIDADE_PADRAO: 5,
    CHAVE_JANELA_INICIO: 6,
    CHAVE_JANELA_FIM: 22,
    CHAVE_MAX_RETRIES: 3,
    CHAVE_RETENCAO_MESES_DADOS: 18,
    CHAVE_RETENCAO_MESES_IMAGENS: 2,
    CHAVE_MAX_TENTATIVAS_ARTIGO_DIA: 3,
}


def _get_valor(chave: str, tipo: str = "inteiro") -> int | float | str | None:
    """Lê valor da config persistida; retorna None se não existir (usa default no caller)."""
    try:
        r = ConfigRegras.query.filter_by(chave=chave).first()
        if not r:
            return None
        if tipo == "inteiro":
            return r.valor_inteiro if r.valor_inteiro is not None else int(r.valor_texto or 0)
        if tipo == "real":
            return r.valor_real if r.valor_real is not None else float(r.valor_texto or 0)
        return r.valor_texto
    except Exception as e:
        logger.warning("Erro ao ler regra %s: %s", chave, e)
        return None


def get_frequencia_horas() -> int:
    """Compatibilidade legada: retorna a frequência arredondada para cima em horas."""
    return max(1, int(math.ceil(get_frequencia_minutos() / 60.0)))


def get_frequencia_minutos() -> int:
    """
    Intervalo em minutos entre ciclos de orquestração.
    Compatível com a configuração legada em horas.
    """
    v = _get_valor(CHAVE_FREQUENCIA_MINUTOS, "inteiro")
    horas = _get_valor(CHAVE_FREQUENCIA_HORAS, "inteiro")
    if (
        v is not None
        and horas is not None
        and int(v) == int(DEFAULTS[CHAVE_FREQUENCIA_MINUTOS])
        and int(horas) != int(DEFAULTS[CHAVE_FREQUENCIA_HORAS])
    ):
        return max(1, int(horas)) * 60
    if v is not None:
        return max(1, int(v))
    if horas is not None:
        return max(1, int(horas)) * 60
    return DEFAULTS[CHAVE_FREQUENCIA_MINUTOS]


def get_prioridade_padrao() -> int:
    """Prioridade padrão para missões (1-10)."""
    v = _get_valor(CHAVE_PRIORIDADE_PADRAO, "inteiro")
    return v if v is not None else DEFAULTS[CHAVE_PRIORIDADE_PADRAO]


def get_janela_publicacao() -> tuple[int, int]:
    """Retorna (hora_inicio, hora_fim) para janela de publicação (0-23)."""
    i = _get_valor(CHAVE_JANELA_INICIO, "inteiro")
    f = _get_valor(CHAVE_JANELA_FIM, "inteiro")
    return (
        i if i is not None else DEFAULTS[CHAVE_JANELA_INICIO],
        f if f is not None else DEFAULTS[CHAVE_JANELA_FIM],
    )


def dentro_janela_publicacao(agora: datetime | None = None) -> bool:
    """True se o momento atual está dentro da janela de publicação configurada."""
    t = agora or datetime.now()
    inicio, fim = get_janela_publicacao()
    hora = t.hour
    if inicio <= fim:
        return inicio <= hora < fim
    return hora >= inicio or hora < fim


def get_max_retries() -> int:
    """Número máximo de tentativas por missão."""
    v = _get_valor(CHAVE_MAX_RETRIES, "inteiro")
    return v if v is not None else DEFAULTS[CHAVE_MAX_RETRIES]


def get_retencao_meses_dados() -> int:
    """Retenção máxima em meses para dados editoriais/coleta."""
    v = _get_valor(CHAVE_RETENCAO_MESES_DADOS, "inteiro")
    return v if v is not None else DEFAULTS[CHAVE_RETENCAO_MESES_DADOS]


def get_retencao_meses_imagens() -> int:
    """Retenção máxima em meses para imagens."""
    v = _get_valor(CHAVE_RETENCAO_MESES_IMAGENS, "inteiro")
    return v if v is not None else DEFAULTS[CHAVE_RETENCAO_MESES_IMAGENS]


def get_max_tentativas_artigo_dia() -> int:
    """
    Limite de tentativas de artigo por dia.
    Usado para evitar loops infinitos de missão de artigo em um mesmo dia.
    """
    v = _get_valor(CHAVE_MAX_TENTATIVAS_ARTIGO_DIA, "inteiro")
    raw = v if v is not None else DEFAULTS[CHAVE_MAX_TENTATIVAS_ARTIGO_DIA]
    try:
        # Bound seguro: entre 1 e 10 tentativas no dia.
        return max(1, min(10, int(raw)))
    except (TypeError, ValueError):
        return DEFAULTS[CHAVE_MAX_TENTATIVAS_ARTIGO_DIA]


def pode_executar_por_frequencia(ultima_execucao: datetime | None, agora: datetime | None = None) -> bool:
    """True se já passou o intervalo de frequência desde a última execução."""
    if ultima_execucao is None:
        return True
    t = agora or _utcnow_naive()
    delta = t - ultima_execucao
    return delta.total_seconds() >= get_frequencia_minutos() * 60


def configurar_frequencia_minutos(valor: int) -> None:
    """
    Persiste a frequência do ciclo em minutos, preservando compatibilidade com a chave legada em horas.
    """
    minutos = max(1, int(valor))
    bootstrap_regras()

    cfg_min = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_MINUTOS).first()
    if not cfg_min:
        cfg_min = ConfigRegras(
            chave=CHAVE_FREQUENCIA_MINUTOS,
            descricao="Intervalo de execução do ciclo em minutos",
        )
        db.session.add(cfg_min)
    cfg_min.valor_inteiro = minutos
    cfg_min.valor_texto = None
    cfg_min.valor_real = None

    cfg_horas = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
    if not cfg_horas:
        cfg_horas = ConfigRegras(
            chave=CHAVE_FREQUENCIA_HORAS,
            descricao="Intervalo de execução do ciclo em horas",
        )
        db.session.add(cfg_horas)
    cfg_horas.valor_inteiro = max(1, int(math.ceil(minutos / 60.0)))
    cfg_horas.valor_texto = None
    cfg_horas.valor_real = None
    db.session.commit()


def bootstrap_regras() -> None:
    """Garante que existam registros padrão em ConfigRegras (idempotente)."""
    try:
        cfg_horas_existente = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
        for chave, valor in DEFAULTS.items():
            r = ConfigRegras.query.filter_by(chave=chave).first()
            if not r:
                r = ConfigRegras(chave=chave, valor_inteiro=valor, descricao=f"Padrão {chave}")
                if (
                    chave == CHAVE_FREQUENCIA_MINUTOS
                    and cfg_horas_existente
                    and cfg_horas_existente.valor_inteiro is not None
                ):
                    r.valor_inteiro = max(1, int(cfg_horas_existente.valor_inteiro)) * 60
                db.session.add(r)
        db.session.commit()
        logger.info("ConfigRegras: bootstrap de chaves padrão concluído.")
    except Exception as e:
        logger.exception("Falha no bootstrap de regras: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
