"""
Serviço de gestão de planos e limites freemium.
Lê e persiste ConfigRegras (histórico chat Júlia e trial) sem hardcode.
"""
import logging
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import ConfigRegras, Conta, Franquia, User
from app.infra import (
    get_freemium_trial_dias,
    CHAVE_JULIA_CHAT_MAX_HISTORY,
    CHAVE_FREEMIUM_TRIAL_DIAS,
)
from app.utils.validators import (
    JULIA_CHAT_MAX_HISTORY_MIN,
    JULIA_CHAT_MAX_HISTORY_MAX,
    FREEMIUM_TRIAL_DIAS_MIN,
    FREEMIUM_TRIAL_DIAS_MAX,
    parse_int_bounded,
)

logger = logging.getLogger(__name__)

DESCRICAO_JULIA_CHAT = "Limite histórico chat Júlia (freemium)"
DESCRICAO_TRIAL_DIAS = "Dias de trial (999999999 = ilimitado)"

PLANOS_SAAS_ADMIN = (
    {"codigo": "free", "nome": "Free", "categorias": ("free",)},
    {"codigo": "starter", "nome": "Starter", "categorias": ("starter", "start", "basico", "básico")},
    {"codigo": "pro", "nome": "Pro", "categorias": ("pro",)},
    {"codigo": "multiuser", "nome": "Multiuser", "categorias": ("multiuser", "enterprise")},
    {"codigo": "avulso", "nome": "Avulso", "categorias": ("avulso",)},
)
_PLANOS_POR_CODIGO = {p["codigo"]: p for p in PLANOS_SAAS_ADMIN}


def _to_decimal(v) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_franquia_limite_raw(raw: str | None) -> Decimal:
    txt = (raw or "").strip().replace(",", ".")
    if not txt:
        raise ValueError("Informe a franquia (limite_total) em créditos.")
    try:
        valor = Decimal(txt)
    except (InvalidOperation, ValueError):
        raise ValueError("Franquia inválida. Use número decimal (ex.: 100 ou 100.5).")
    if valor <= 0:
        raise ValueError("Franquia deve ser maior que zero.")
    return valor.quantize(Decimal("0.000001"))


def _parse_valor_plano_raw(raw: str | None) -> Decimal:
    txt = (raw or "").strip().replace(",", ".")
    if not txt:
        raise ValueError("Informe o valor do plano em reais (R$).")
    try:
        valor = Decimal(txt)
    except (InvalidOperation, ValueError):
        raise ValueError("Valor do plano inválido. Use formato decimal (ex.: 99.90).")
    if valor < 0:
        raise ValueError("Valor do plano não pode ser negativo.")
    return valor.quantize(Decimal("0.01"))


def _config_key_valor_plano(plano_codigo: str) -> str:
    return f"plano_valor_admin_{plano_codigo}"


def _config_key_franquia_ref(plano_codigo: str) -> str:
    return f"plano_franquia_ref_admin_{plano_codigo}"


def _config_desc_valor_plano(plano_nome: str) -> str:
    return f"Valor administrativo do plano {plano_nome} (R$)"


def obter_limite_referencia_plano_admin(
    plano_codigo: str,
    *,
    exigir_configurado: bool = True,
) -> Decimal | None:
    """
    Porta canônica para leitura do limite de franquia de referência de um plano comercial.
    Fonte: ConfigRegras (chave plano_franquia_ref_admin_<codigo>) -> Franquia.limite_total.
    """
    codigo = (plano_codigo or "").strip().lower()
    plano = _PLANOS_POR_CODIGO.get(codigo)
    if not plano:
        raise ValueError(f"Plano '{plano_codigo}' não encontrado na configuração administrativa.")

    cfg_ref_key = _config_key_franquia_ref(codigo)
    cfg_ref = ConfigRegras.query.filter_by(chave=cfg_ref_key).first()
    ref_id = cfg_ref.valor_inteiro if cfg_ref else None
    if not ref_id:
        if exigir_configurado:
            raise ValueError(
                f"Franquia de referência do plano {codigo} não está configurada em /admin/planos."
            )
        return None

    franquia_ref = db.session.get(Franquia, int(ref_id))
    if franquia_ref is None:
        raise ValueError(
            f"Franquia de referência do plano {codigo} não encontrada (id={ref_id})."
        )

    limite = _to_decimal(franquia_ref.limite_total)
    if exigir_configurado and limite is None:
        raise ValueError(
            f"Limite da franquia de referência do plano {codigo} está vazio em /admin/planos."
        )
    return limite


def corrigir_franquias_free_sem_limite() -> dict:
    """
    Correção pontual para legado: preenche limite_total nulo em franquias de usuários free
    (exceto estrutura interna/sistema), usando a referência administrativa vigente do plano free.
    """
    from app.services.conta_franquia_service import get_sistema_interno_ids

    limite_free = obter_limite_referencia_plano_admin("free", exigir_configurado=True)
    sistema_conta_id, sistema_franquia_id = get_sistema_interno_ids()

    franquias = (
        db.session.query(Franquia)
        .join(User, User.franquia_id == Franquia.id)
        .filter(db.func.lower(User.categoria) == "free")
        .filter(Franquia.limite_total.is_(None))
        .distinct(Franquia.id)
        .all()
    )
    atualizadas = 0
    for fr in franquias:
        if (
            (sistema_conta_id is not None and fr.conta_id == int(sistema_conta_id))
            or (sistema_franquia_id is not None and fr.id == int(sistema_franquia_id))
        ):
            continue
        fr.limite_total = limite_free
        db.session.add(fr)
        atualizadas += 1

    if atualizadas:
        db.session.commit()

    return {
        "limite_aplicado": limite_free,
        "franquias_free_sem_limite_encontradas": len(franquias),
        "franquias_free_atualizadas": atualizadas,
    }


def listar_planos_saas_admin() -> list[dict]:
    """
    Retorna payload da seção Gestão de Planos & SaaS com foco em Franquia operacional.
    """
    cfg_keys: list[str] = []
    for p in PLANOS_SAAS_ADMIN:
        cfg_keys.append(_config_key_valor_plano(p["codigo"]))
        cfg_keys.append(_config_key_franquia_ref(p["codigo"]))
    cfg_rows = (
        ConfigRegras.query.filter(ConfigRegras.chave.in_(cfg_keys)).all()
    )
    cfg_map = {row.chave: row for row in cfg_rows}

    planos: list[dict] = []
    for p in PLANOS_SAAS_ADMIN:
        cfg_valor = cfg_map.get(_config_key_valor_plano(p["codigo"]))
        valor_admin = (
            _to_decimal(cfg_valor.valor_real)
            if cfg_valor and cfg_valor.valor_real is not None
            else None
        )
        categorias = tuple(c.lower() for c in p["categorias"])
        franquias_usuarios = (
            db.session.query(Franquia)
            .join(User, User.franquia_id == Franquia.id)
            .filter(db.func.lower(User.categoria).in_(categorias))
            .distinct(Franquia.id)
            .all()
        )
        cfg_franquia_ref = cfg_map.get(_config_key_franquia_ref(p["codigo"]))
        franquias_map = {fr.id: fr for fr in franquias_usuarios}
        ref_id = cfg_franquia_ref.valor_inteiro if cfg_franquia_ref else None
        if ref_id:
            fr_ref = db.session.get(Franquia, int(ref_id))
            if fr_ref is not None:
                franquias_map[fr_ref.id] = fr_ref
        franquias = list(franquias_map.values())
        limites = [
            valor for valor in (_to_decimal(fr.limite_total) for fr in franquias)
            if valor is not None
        ]
        limite_referencia = max(limites) if limites else None
        planos.append(
            {
                "codigo": p["codigo"],
                "nome": p["nome"],
                "valor_admin": valor_admin,
                "franquias_vinculadas": len(franquias),
                "franquias_com_limite": len(limites),
                "franquia_referencia": limite_referencia,
            }
        )
    return planos


def atualizar_parametros_plano_admin(
    plano_codigo: str,
    valor_plano_raw: str | None,
    franquia_limite_total_raw: str | None,
) -> dict:
    """
    Atualiza parâmetros administrativos do plano:
    - valor comercial admin em ConfigRegras
    - franquia operacional em Franquia.limite_total
    """
    codigo = (plano_codigo or "").strip().lower()
    plano = _PLANOS_POR_CODIGO.get(codigo)
    if not plano:
        raise ValueError("Plano inválido para atualização.")

    valor_plano = _parse_valor_plano_raw(valor_plano_raw)
    novo_limite = _parse_franquia_limite_raw(franquia_limite_total_raw)
    categorias = tuple(c.lower() for c in plano["categorias"])
    franquias = (
        db.session.query(Franquia)
        .join(User, User.franquia_id == Franquia.id)
        .filter(db.func.lower(User.categoria).in_(categorias))
        .distinct(Franquia.id)
        .all()
    )

    cfg_key = _config_key_valor_plano(codigo)
    cfg = ConfigRegras.query.filter_by(chave=cfg_key).first()
    if not cfg:
        cfg = ConfigRegras(
            chave=cfg_key,
            descricao=_config_desc_valor_plano(plano["nome"]),
        )
        db.session.add(cfg)

    cfg.valor_real = float(valor_plano)
    cfg.valor_texto = str(valor_plano)
    cfg.valor_inteiro = None

    cfg_ref_key = _config_key_franquia_ref(codigo)
    cfg_ref = ConfigRegras.query.filter_by(chave=cfg_ref_key).first()
    if not cfg_ref:
        cfg_ref = ConfigRegras(
            chave=cfg_ref_key,
            descricao=f"Franquia de referência administrativa do plano {plano['nome']}",
        )
        db.session.add(cfg_ref)

    franquia_ref = None
    if cfg_ref.valor_inteiro:
        franquia_ref = db.session.get(Franquia, int(cfg_ref.valor_inteiro))
    if franquia_ref is None and franquias:
        franquia_ref = franquias[0]

    atualizadas = 0
    try:
        if franquia_ref is None:
            conta_sistema = Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first()
            if not conta_sistema:
                raise ValueError(
                    "Conta interna de referência não encontrada para criar franquia do plano."
                )
            slug_base = f"ref-plano-{codigo}"[:80]
            slug_final = slug_base
            sufixo = 1
            while Franquia.query.filter_by(conta_id=conta_sistema.id, slug=slug_final).first():
                base = f"{slug_base[:70]}-{sufixo}"
                slug_final = base[:80]
                sufixo += 1
            franquia_ref = Franquia(
                conta_id=conta_sistema.id,
                nome=f"Referência {plano['nome']}",
                slug=slug_final,
                status=Franquia.STATUS_ACTIVE,
            )
            db.session.add(franquia_ref)
            db.session.flush()

        franquias_para_aplicar = {fr.id: fr for fr in franquias}
        if franquia_ref is not None:
            franquias_para_aplicar[franquia_ref.id] = franquia_ref
            cfg_ref.valor_inteiro = int(franquia_ref.id)
            cfg_ref.valor_texto = str(franquia_ref.id)
            cfg_ref.valor_real = None
        else:
            cfg_ref.valor_inteiro = None
            cfg_ref.valor_texto = None
            cfg_ref.valor_real = None
        db.session.add(cfg_ref)

        for fr in franquias_para_aplicar.values():
            if _to_decimal(fr.limite_total) != novo_limite:
                fr.limite_total = novo_limite
                db.session.add(fr)
                atualizadas += 1
        db.session.commit()
    except Exception as e:
        logger.exception(
            "Erro ao atualizar franquia de referência do plano %s: %s",
            codigo,
            e,
        )
        db.session.rollback()
        raise

    return {
        "plano_nome": plano["nome"],
        "valor_plano": valor_plano,
        "franquia_limite_total": novo_limite,
        "franquias_total": len(franquias_para_aplicar),
        "franquias_atualizadas": atualizadas,
    }


def obter_config_planos():
    """
    Retorna dict com configuração atual de planos para exibição no admin:
    limites freemium lidos do infra/ConfigRegras e planos SaaS com referência em Franquia.
    """
    return {
        "planos_saas": listar_planos_saas_admin(),
        "freemium_trial_dias": get_freemium_trial_dias(),
    }


def salvar_julia_chat_max_history(julia_chat_max_history_raw: str | None) -> int | None:
    """
    Persiste em ConfigRegras o limite de histórico do chat Júlia.
    Retorna o valor salvo; None para entrada vazia/inválida.
    """
    if not julia_chat_max_history_raw:
        return None
    v = parse_int_bounded(
        julia_chat_max_history_raw,
        JULIA_CHAT_MAX_HISTORY_MIN,
        JULIA_CHAT_MAX_HISTORY_MAX,
    )
    if v is None:
        return None
    try:
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
        db.session.commit()
    except Exception as e:
        logger.exception("Erro ao salvar limite de histórico Júlia: %s", e)
        db.session.rollback()
        raise
    return v


def salvar_freemium_trial_dias(freemium_trial_dias_raw: str | None) -> int | None:
    """
    Persiste em ConfigRegras os dias de trial do plano Free.
    Retorna o valor salvo; None para entrada vazia/inválida.
    """
    if not freemium_trial_dias_raw:
        return None
    v = parse_int_bounded(
        freemium_trial_dias_raw,
        FREEMIUM_TRIAL_DIAS_MIN,
        FREEMIUM_TRIAL_DIAS_MAX,
    )
    if v is None:
        return None
    try:
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
        db.session.commit()
    except Exception as e:
        logger.exception("Erro ao salvar dias de trial: %s", e)
        db.session.rollback()
        raise
    return v
