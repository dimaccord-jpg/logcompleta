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
PLANOS_GATEWAY_MONETIZACAO = ("starter", "pro")

_GATEWAY_PROVIDER_OPCOES = {"stripe"}
_GATEWAY_INTERVALO_OPCOES = {"month", "year"}
_GATEWAY_CURRENCY_OPCOES = {"brl", "usd", "eur"}


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


def _config_key_gateway_provider(plano_codigo: str) -> str:
    return f"plano_gateway_provider_admin_{plano_codigo}"


def _config_key_gateway_product_id(plano_codigo: str) -> str:
    return f"plano_gateway_product_id_admin_{plano_codigo}"


def _config_key_gateway_price_id(plano_codigo: str) -> str:
    return f"plano_gateway_price_id_admin_{plano_codigo}"


def _config_key_gateway_currency(plano_codigo: str) -> str:
    return f"plano_gateway_currency_admin_{plano_codigo}"


def _config_key_gateway_interval(plano_codigo: str) -> str:
    return f"plano_gateway_interval_admin_{plano_codigo}"


def _config_key_gateway_ready(plano_codigo: str) -> str:
    return f"plano_gateway_ready_admin_{plano_codigo}"


def _bool_from_raw(raw: str | bool | None) -> bool:
    if isinstance(raw, bool):
        return raw
    txt = (raw or "").strip().lower()
    return txt in {"1", "true", "yes", "on"}


def _normalizar_gateway_provider(raw: str | None) -> str | None:
    txt = (raw or "").strip().lower()
    if not txt:
        return None
    if txt not in _GATEWAY_PROVIDER_OPCOES:
        raise ValueError(
            "Provider externo inválido. Valores aceitos nesta fase: stripe."
        )
    return txt


def _normalizar_gateway_product_id(raw: str | None) -> str | None:
    txt = (raw or "").strip()
    return txt or None


def _normalizar_gateway_price_id(raw: str | None) -> str | None:
    txt = (raw or "").strip()
    return txt or None


def _normalizar_gateway_currency(raw: str | None) -> str | None:
    txt = (raw or "").strip().lower()
    if not txt:
        return None
    if txt not in _GATEWAY_CURRENCY_OPCOES:
        raise ValueError("Moeda inválida para gateway. Use brl, usd ou eur.")
    return txt


def _normalizar_gateway_interval(raw: str | None) -> str | None:
    txt = (raw or "").strip().lower()
    if not txt:
        return None
    if txt not in _GATEWAY_INTERVALO_OPCOES:
        raise ValueError("Periodicidade inválida para gateway. Use month ou year.")
    return txt


def _obter_cfg_map(chaves: list[str]) -> dict[str, ConfigRegras]:
    if not chaves:
        return {}
    rows = ConfigRegras.query.filter(ConfigRegras.chave.in_(chaves)).all()
    return {row.chave: row for row in rows}


def _upsert_cfg_texto(cfg_map: dict[str, ConfigRegras], chave: str, descricao: str, valor: str | None) -> None:
    cfg = cfg_map.get(chave)
    if not cfg:
        cfg = ConfigRegras(chave=chave, descricao=descricao)
        db.session.add(cfg)
        cfg_map[chave] = cfg
    cfg.valor_texto = valor
    cfg.valor_real = None
    cfg.valor_inteiro = None
    db.session.add(cfg)


def _upsert_cfg_bool(cfg_map: dict[str, ConfigRegras], chave: str, descricao: str, valor: bool) -> None:
    cfg = cfg_map.get(chave)
    if not cfg:
        cfg = ConfigRegras(chave=chave, descricao=descricao)
        db.session.add(cfg)
        cfg_map[chave] = cfg
    cfg.valor_inteiro = 1 if valor else 0
    cfg.valor_texto = "1" if valor else "0"
    cfg.valor_real = None
    db.session.add(cfg)


def _ler_config_gateway_plano(cfg_map: dict[str, ConfigRegras], plano_codigo: str) -> dict:
    provider = (cfg_map.get(_config_key_gateway_provider(plano_codigo)).valor_texto if cfg_map.get(_config_key_gateway_provider(plano_codigo)) else None)
    product_id = (cfg_map.get(_config_key_gateway_product_id(plano_codigo)).valor_texto if cfg_map.get(_config_key_gateway_product_id(plano_codigo)) else None)
    price_id = (cfg_map.get(_config_key_gateway_price_id(plano_codigo)).valor_texto if cfg_map.get(_config_key_gateway_price_id(plano_codigo)) else None)
    currency = (cfg_map.get(_config_key_gateway_currency(plano_codigo)).valor_texto if cfg_map.get(_config_key_gateway_currency(plano_codigo)) else None)
    interval = (cfg_map.get(_config_key_gateway_interval(plano_codigo)).valor_texto if cfg_map.get(_config_key_gateway_interval(plano_codigo)) else None)
    cfg_ready = cfg_map.get(_config_key_gateway_ready(plano_codigo))
    pronto = bool(cfg_ready and (cfg_ready.valor_inteiro or 0) == 1)

    pendencias: list[str] = []
    if not provider:
        pendencias.append("gateway_provider_nao_configurado")
    if not product_id:
        pendencias.append("gateway_product_id_nao_configurado")
    if not price_id:
        pendencias.append("gateway_price_id_nao_configurado")
    if not currency:
        pendencias.append("gateway_currency_nao_configurado")
    if not interval:
        pendencias.append("gateway_interval_nao_configurado")
    if not pronto:
        pendencias.append("gateway_pronto_desmarcado")

    configuracao_valida = len(pendencias) == 0
    return {
        "provider": provider,
        "product_id": product_id,
        "price_id": price_id,
        "currency": currency,
        "interval": interval,
        "pronto": pronto,
        "configuracao_valida": configuracao_valida,
        "pendencias": pendencias,
    }


def obter_nome_exibivel_plano(plano_codigo: str | None) -> str:
    """
    Nome amigável canônico para exibição de plano em mensagens de produto.
    """
    codigo = (plano_codigo or "").strip().lower()
    plano = _PLANOS_POR_CODIGO.get(codigo)
    if plano:
        return plano["nome"]
    if codigo:
        return codigo.capitalize()
    return "atual"


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
        .distinct()
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
        if p["codigo"] in PLANOS_GATEWAY_MONETIZACAO:
            cfg_keys.extend(
                [
                    _config_key_gateway_provider(p["codigo"]),
                    _config_key_gateway_product_id(p["codigo"]),
                    _config_key_gateway_price_id(p["codigo"]),
                    _config_key_gateway_currency(p["codigo"]),
                    _config_key_gateway_interval(p["codigo"]),
                    _config_key_gateway_ready(p["codigo"]),
                ]
            )
    cfg_map = _obter_cfg_map(cfg_keys)

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
            .distinct()
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
                "gateway_config": (
                    _ler_config_gateway_plano(cfg_map, p["codigo"])
                    if p["codigo"] in PLANOS_GATEWAY_MONETIZACAO
                    else None
                ),
            }
        )
    return planos


def atualizar_parametros_plano_admin(
    plano_codigo: str,
    valor_plano_raw: str | None,
    franquia_limite_total_raw: str | None,
    gateway_provider_raw: str | None = None,
    gateway_product_id_raw: str | None = None,
    gateway_price_id_raw: str | None = None,
    gateway_currency_raw: str | None = None,
    gateway_interval_raw: str | None = None,
    gateway_pronto_raw: str | bool | None = None,
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
    gateway_provider = _normalizar_gateway_provider(gateway_provider_raw)
    gateway_product_id = _normalizar_gateway_product_id(gateway_product_id_raw)
    gateway_price_id = _normalizar_gateway_price_id(gateway_price_id_raw)
    gateway_currency = _normalizar_gateway_currency(gateway_currency_raw)
    gateway_interval = _normalizar_gateway_interval(gateway_interval_raw)
    gateway_pronto = _bool_from_raw(gateway_pronto_raw)
    categorias = tuple(c.lower() for c in plano["categorias"])
    franquias = (
        db.session.query(Franquia)
        .join(User, User.franquia_id == Franquia.id)
        .filter(db.func.lower(User.categoria).in_(categorias))
        .distinct()
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

    gateway_cfg_chaves = []
    if codigo in PLANOS_GATEWAY_MONETIZACAO:
        gateway_cfg_chaves = [
            _config_key_gateway_provider(codigo),
            _config_key_gateway_product_id(codigo),
            _config_key_gateway_price_id(codigo),
            _config_key_gateway_currency(codigo),
            _config_key_gateway_interval(codigo),
            _config_key_gateway_ready(codigo),
        ]
    gateway_cfg_map = _obter_cfg_map(gateway_cfg_chaves)

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

        if codigo in PLANOS_GATEWAY_MONETIZACAO:
            _upsert_cfg_texto(
                gateway_cfg_map,
                _config_key_gateway_provider(codigo),
                f"Provider externo do plano {plano['nome']}",
                gateway_provider,
            )
            _upsert_cfg_texto(
                gateway_cfg_map,
                _config_key_gateway_product_id(codigo),
                f"Product ID externo do plano {plano['nome']}",
                gateway_product_id,
            )
            _upsert_cfg_texto(
                gateway_cfg_map,
                _config_key_gateway_price_id(codigo),
                f"Price ID externo ativo do plano {plano['nome']}",
                gateway_price_id,
            )
            _upsert_cfg_texto(
                gateway_cfg_map,
                _config_key_gateway_currency(codigo),
                f"Moeda externa do plano {plano['nome']}",
                gateway_currency,
            )
            _upsert_cfg_texto(
                gateway_cfg_map,
                _config_key_gateway_interval(codigo),
                f"Periodicidade externa do plano {plano['nome']}",
                gateway_interval,
            )
            _upsert_cfg_bool(
                gateway_cfg_map,
                _config_key_gateway_ready(codigo),
                f"Indicador de prontidao de configuracao externa do plano {plano['nome']}",
                gateway_pronto,
            )

        db.session.commit()
    except Exception as e:
        logger.exception(
            "Erro ao atualizar franquia de referência do plano %s: %s",
            codigo,
            e,
        )
        db.session.rollback()
        raise

    gateway_config = None
    if codigo in PLANOS_GATEWAY_MONETIZACAO:
        gateway_config = _ler_config_gateway_plano(
            _obter_cfg_map(gateway_cfg_chaves),
            codigo,
        )

    return {
        "plano_nome": plano["nome"],
        "valor_plano": valor_plano,
        "franquia_limite_total": novo_limite,
        "franquias_total": len(franquias_para_aplicar),
        "franquias_atualizadas": atualizadas,
        "gateway_config": gateway_config,
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


def listar_pendencias_gateway_monetizacao_admin() -> list[dict]:
    """
    Retorna pendencias explicitas de configuracao de gateway para starter/pro.
    Nao bloqueia operacao; apenas sinaliza estado administrativo.
    """
    planos = listar_planos_saas_admin()
    pendencias: list[dict] = []
    for plano in planos:
        if plano["codigo"] not in PLANOS_GATEWAY_MONETIZACAO:
            continue
        gateway = plano.get("gateway_config") or {}
        for pendencia in gateway.get("pendencias", []):
            pendencias.append(
                {
                    "plano_codigo": plano["codigo"],
                    "plano_nome": plano["nome"],
                    "pendencia": pendencia,
                }
            )
    return pendencias


def listar_pendencias_gateway_monetizacao_por_plano_admin(
    plano_codigo: str | None,
) -> list[dict]:
    """
    Retorna pendencias de configuracao externa apenas do plano informado.
    Se o plano nao participa do escopo Stripe da fase 1, retorna lista vazia.
    """
    codigo = (plano_codigo or "").strip().lower()
    if codigo not in PLANOS_GATEWAY_MONETIZACAO:
        return []
    planos = listar_planos_saas_admin()
    for plano in planos:
        if plano.get("codigo") != codigo:
            continue
        gateway = plano.get("gateway_config") or {}
        return [
            {
                "plano_codigo": plano["codigo"],
                "plano_nome": plano["nome"],
                "pendencia": pendencia,
            }
            for pendencia in gateway.get("pendencias", [])
        ]
    return [
        {
            "plano_codigo": codigo,
            "plano_nome": codigo.capitalize(),
            "pendencia": "plano_nao_encontrado_config_admin",
        }
    ]


def obter_configuracao_gateway_plano_admin(plano_codigo: str | None) -> dict | None:
    """
    Retorna configuracao de gateway do plano informado no formato consolidado do admin.
    Retorna None quando o plano nao participa do escopo de monetizacao recorrente.
    """
    codigo = (plano_codigo or "").strip().lower()
    if codigo not in PLANOS_GATEWAY_MONETIZACAO:
        return None
    planos = listar_planos_saas_admin()
    for plano in planos:
        if plano.get("codigo") == codigo:
            gateway_cfg = dict(plano.get("gateway_config") or {})
            gateway_cfg["plano_codigo"] = plano.get("codigo")
            gateway_cfg["plano_nome"] = plano.get("nome")
            return gateway_cfg
    return None


def resolver_plano_por_gateway_price_id_admin(
    *,
    provider: str | None,
    price_id: str | None,
) -> dict | None:
    """
    Resolve o plano interno por price_id configurado no admin.
    Usado para correlacao de eventos Stripe sem hardcode de plano.
    """
    provider_n = (provider or "").strip().lower()
    price_id_n = (price_id or "").strip()
    if provider_n != "stripe" or not price_id_n:
        return None
    for plano in listar_planos_saas_admin():
        if plano.get("codigo") not in PLANOS_GATEWAY_MONETIZACAO:
            continue
        gateway = plano.get("gateway_config") or {}
        if (gateway.get("provider") or "").strip().lower() != provider_n:
            continue
        if (gateway.get("price_id") or "").strip() != price_id_n:
            continue
        return {
            "plano_codigo": plano.get("codigo"),
            "plano_nome": plano.get("nome"),
            "gateway_config": gateway,
        }
    return None


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
