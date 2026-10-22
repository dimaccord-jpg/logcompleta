"""
Cobrança da extração de tabela do AgenteCompara.

A chamada observa o consumo e não reserva crédito. O débito acontece uma vez,
depois da entrega: a tabela precisa ter valor de frete e contexto tarifário
suficiente para uso, estar persistida e ficar disponível para revisão. O status
needs_review sozinho não comprova utilidade. Tentativa sem entrega aproveitável
permanece no evento de consumo e não abate a franquia.
"""
from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

FLOW_EXTRACAO_TABELA = "agente_compara_temp_table_extraction"
_MARCADOR_ENTREGA = "ac-tt:"

_STATUS_REVISAVEL = frozenset({"needs_review", "awaiting_validation", "validated"})


def digest_da_entrega(
    source_doc_ids: list[str],
    *,
    comparison_id: str | None,
    table_id: str | None,
) -> str:
    from app.agente_compara_doc_service import agente_compara_temp_table_extraction_idempotency_key

    bruto = agente_compara_temp_table_extraction_idempotency_key(
        source_doc_ids,
        comparison_id=comparison_id,
        table_id=table_id,
    )
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:32]


_DEFEITOS_SEM_FRETE_UTIL = frozenset(
    {
        "no_executable_rule",
        "missing_lookup_key",
        "unresolved_pricing_type",
        "invalid_tariff",
        "invalid_range",
        "invalid_excess",
        "missing_unit",
        "freight_value_not_executable",
    }
)


def _contexto_tarifario_suficiente(rule: dict) -> bool:
    """Destino, praça ou rota. Rótulo de coluna de valor, como Frete, não localiza a tarifa."""
    from app.agente_compara_doc_service import _is_value_column

    keys = [
        key.strip()
        for key in (rule.get("lookup_keys") or [])
        if isinstance(key, str) and key.strip()
    ]
    if not keys:
        return False
    return any(not _is_value_column(key) for key in keys)


def _valor_de_frete_executavel(rule: dict) -> bool:
    from app.agente_compara_doc_service import _is_finite_number

    pricing_type = rule.get("pricing_type")
    if pricing_type == "direct_weight_rate":
        unit = str(rule.get("unit") or "").strip().lower()
        amount = rule.get("value_per_ton") if unit == "ton" else rule.get("value_per_kg")
        return _is_finite_number(amount) and float(amount) >= 0
    if pricing_type in {"fixed_range", "range_plus_excess_per_kg"}:
        brackets = rule.get("brackets") if isinstance(rule.get("brackets"), list) else []
        return any(
            isinstance(item, dict)
            and _is_finite_number(item.get("value"))
            and float(item.get("value")) >= 0
            and _is_finite_number(item.get("max_kg"))
            for item in brackets
        )
    return False


def _regra_comercialmente_aplicavel(rule: dict) -> bool:
    from app.agente_compara_temp_table_validation_service import pricing_contract_structure_findings

    if not isinstance(rule, dict):
        return False
    if not _contexto_tarifario_suficiente(rule) or not _valor_de_frete_executavel(rule):
        return False
    try:
        findings = pricing_contract_structure_findings({"rules": [rule]})
    except Exception:
        return False
    return not any(
        isinstance(item, dict) and item.get("kind") in _DEFEITOS_SEM_FRETE_UTIL
        for item in findings
    )


def estrutura_tarifaria_aproveitavel(registro: dict) -> bool:
    """Valor de frete executável com contexto suficiente para uso.

    Destino isolado, rota sem tarifa, rótulo Frete, faixa sem preço, pedágio
    ou acessório isolado e JSON sem regra utilizável não autorizam débito.
    """
    from app.agente_compara_doc_service import _compile_pricing_contract

    if not isinstance(registro, dict):
        return False
    try:
        contract, _findings = _compile_pricing_contract(registro)
    except Exception:
        return False
    rules = contract.get("rules") if isinstance(contract, dict) else None
    if not isinstance(rules, list):
        return False
    return any(_regra_comercialmente_aplicavel(rule) for rule in rules if isinstance(rule, dict))


def entrega_faturavel(registro: dict | None) -> bool:
    """Tabela persistida, revisável e com estrutura tarifária aproveitável."""
    if not isinstance(registro, dict):
        return False
    identificador = registro.get("temp_table_id")
    if not isinstance(identificador, str) or not identificador.strip():
        return False
    status = registro.get("status")
    if not isinstance(status, str) or status.strip().lower() not in _STATUS_REVISAVEL:
        return False
    return estrutura_tarifaria_aproveitavel(registro)


class ExtracaoRecusada(Exception):
    """Recusa antes da geração. Não reserva crédito e não cria tentativa."""

    def __init__(self, codigo: str) -> None:
        super().__init__(codigo)
        self.codigo = codigo


class ExtracaoConsumoIncerto(Exception):
    """A entrega útil ficou sem débito confirmado. Não há reserva pendente."""


def _inteiro_de_orcamento(valor: int | None) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None
    return valor


@dataclass
class GeracaoExtracao:
    response: Any
    evento_id: int | None


def _saldo_positivo(franquia) -> bool:
    from app.services.cleiton_franquia_operacional_service import saldo_disponivel_chamada_ia

    saldo = saldo_disponivel_chamada_ia(franquia)
    if saldo is None:
        return True
    return saldo > 0


def _identidade_autorizada(usuario: Any) -> dict[str, Any]:
    """Identidade e franquia antes do Gemini. Saldo positivo autoriza, sem teto de reserva."""
    from app.extensions import db
    from app.models import Franquia
    from app.services.cleiton_billable_ai_call import _resolver_identidade
    from app.services.cleiton_operacao_autorizacao_service import (
        avaliar_autorizacao_operacao_por_franquia,
    )

    ident, motivo = _resolver_identidade(
        usuario=usuario,
        conta_id=None,
        franquia_id=None,
        usuario_id=None,
        origem_sistema=False,
    )
    if motivo or ident is None:
        raise ExtracaoRecusada("operacao")
    franquia = db.session.get(Franquia, int(ident["franquia_id"]))
    if franquia is None:
        raise ExtracaoRecusada("operacao")
    try:
        authz = avaliar_autorizacao_operacao_por_franquia(usuario, sincronizar_ciclo=False)
    except Exception:
        raise ExtracaoRecusada("operacao") from None
    if not isinstance(authz, dict) or authz.get("permitido") is not True:
        motivo_auth = str((authz or {}).get("motivo") or "")
        if motivo_auth.startswith("limite_atingido") or motivo_auth == "limite_ou_vigencia_avulso":
            raise ExtracaoRecusada("creditos")
        raise ExtracaoRecusada("operacao")
    if not _saldo_positivo(franquia):
        raise ExtracaoRecusada("creditos")
    return ident


def cleiton_governed_generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    agent: str,
    flow_type: str,
    api_key_label: str,
    config: Any = None,
    usuario: Any = None,
    output_token_cap: int | None = None,
    thinking_token_cap: int | None = None,
    operation_token_budget: int | None = None,
    **_ignorado: Any,
) -> GeracaoExtracao:
    """
    Mesma preparação de saída e thinking da extração, sem reserva e sem tentativa.

    A autorização valida identidade e franquia antes da contagem e da geração.
    O orçamento da chamada é o teto técnico restante já limitado pela capacidade
    do modelo. O saldo de créditos não altera saída nem thinking.
    """
    from flask import has_app_context

    from app.consumo_identidade import (
        clear_consumo_identidade,
        get_consumo_identidade,
        set_consumo_identidade,
    )
    from app.run_cleiton_gemini_governance import (
        PERSIST_OK,
        STATUS_FAILURE,
        STATUS_SUCCESS_NO_METRICS,
        cleiton_governed_generate_content_observed,
    )
    from app.services.agente_compara_extraction_budget import encaixar_limites_na_operacao
    from app.services.cleiton_ai_data_governance import (
        CleitonAiGovernanceBlockedError,
        purpose_from_flow_type,
    )
    from app.services.cleiton_billable_ai_call import (
        MOTIVO_PERSISTENCIA_EVENTO,
        MOTIVO_PROVIDER_SEM_USAGE,
        MOTIVO_SUCCESS_NO_METRICS,
        SDK_RETRY_ATTEMPTS,
        BillableAiUncertainError,
        _payload_mutavel_depois_da_contagem,
        _payload_seguro_para_metering,
        _preparar_config_limitada,
        contar_tokens_oficiais,
        resolver_limites_config,
        tentativas_retry_config,
        tentativas_retry_sdk,
    )

    if not has_app_context():
        raise ExtracaoRecusada("operacao")
    ident = _identidade_autorizada(usuario)
    if tentativas_retry_sdk(client) != SDK_RETRY_ATTEMPTS:
        raise ExtracaoRecusada("operacao")
    if tentativas_retry_config(config) not in (None, SDK_RETRY_ATTEMPTS):
        raise ExtracaoRecusada("operacao")
    orcamento = _inteiro_de_orcamento(operation_token_budget)
    if orcamento is None:
        raise ExtracaoRecusada("operacao")

    saida, candidatos, thinking, erro_limite = resolver_limites_config(
        config,
        output_token_cap=output_token_cap,
        thinking_token_cap=thinking_token_cap,
    )
    if erro_limite:
        raise ExtracaoRecusada("operacao")
    if _payload_mutavel_depois_da_contagem(config, client):
        raise ExtracaoRecusada("operacao")

    resolved_purpose = purpose_from_flow_type(flow_type, agent)
    try:
        safe_contents, safe_config = _payload_seguro_para_metering(
            contents,
            config,
            purpose=resolved_purpose,
            agent=agent,
        )
    except CleitonAiGovernanceBlockedError as exc:
        raise ExtracaoRecusada("operacao") from exc

    config_cap, teto_ok, _motivo_cfg = _preparar_config_limitada(
        safe_config,
        saida=saida,
        candidatos=candidatos,
        thinking=thinking,
    )
    if not teto_ok:
        raise ExtracaoRecusada("operacao")
    try:
        conteudo_contado = copy.deepcopy(safe_contents)
        conteudo_gerado = copy.deepcopy(safe_contents)
        config_contagem = copy.deepcopy(config_cap)
    except Exception as exc:
        raise ExtracaoRecusada("operacao") from exc

    entrada, erro_count = contar_tokens_oficiais(
        client,
        model=model,
        contents=conteudo_contado,
        config=config_contagem,
        agent=agent,
        flow_type=flow_type,
    )
    if erro_count:
        raise ExtracaoRecusada("operacao")
    encaixe = encaixar_limites_na_operacao(
        entrada,
        saida,
        thinking,
        orcamento,
        candidatos=candidatos,
        model=model,
    )
    orcamento_efetivo = orcamento
    if encaixe is None:
        logger.info(
            "Agente Compara extracao teto tecnico model=%s autorizado=%s entrada=%s",
            model,
            orcamento_efetivo,
            entrada,
        )
        raise ExtracaoRecusada("teto")
    saida, thinking = encaixe
    config_cap, teto_ok, _motivo_cfg = _preparar_config_limitada(
        safe_config,
        saida=saida,
        candidatos=candidatos,
        thinking=thinking,
    )
    if not teto_ok:
        raise ExtracaoRecusada("operacao")
    try:
        config_geracao = copy.deepcopy(config_cap)
    except Exception as exc:
        raise ExtracaoRecusada("operacao") from exc
    logger.info(
        "Agente Compara extracao geracao model=%s autorizado=%s entrada=%s saida=%s thinking=%s",
        model,
        orcamento_efetivo,
        entrada,
        saida,
        thinking,
    )

    anterior = get_consumo_identidade()
    if ident is not None:
        set_consumo_identidade(ident)
    try:
        observation = cleiton_governed_generate_content_observed(
            client,
            model=model,
            contents=conteudo_gerado,
            config=config_geracao,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            purpose=resolved_purpose,
            identidade=ident,
            apply_motor=False,
        )
    except CleitonAiGovernanceBlockedError as exc:
        raise ExtracaoRecusada("operacao") from exc
    finally:
        if anterior is None:
            clear_consumo_identidade()
        else:
            set_consumo_identidade(anterior)

    if observation.persist_status != PERSIST_OK or observation.evento_id is None:
        raise BillableAiUncertainError(
            MOTIVO_PERSISTENCIA_EVENTO,
            motivo=MOTIVO_PERSISTENCIA_EVENTO,
            provider_called=observation.provider_called,
            response=observation.response,
        )
    if observation.status == STATUS_SUCCESS_NO_METRICS:
        raise BillableAiUncertainError(
            MOTIVO_SUCCESS_NO_METRICS,
            motivo=MOTIVO_SUCCESS_NO_METRICS,
            provider_called=True,
            response=observation.response,
        )
    sem_usage = (
        observation.input_tokens is None
        and observation.output_tokens is None
        and observation.total_tokens is None
    )
    if observation.status == STATUS_FAILURE and sem_usage:
        raise BillableAiUncertainError(
            MOTIVO_PROVIDER_SEM_USAGE,
            motivo=MOTIVO_PROVIDER_SEM_USAGE,
            provider_called=True,
        ) from observation.provider_exception
    if observation.provider_exception is not None:
        raise observation.provider_exception
    if observation.response is None:
        raise BillableAiUncertainError(
            MOTIVO_PERSISTENCIA_EVENTO,
            motivo=MOTIVO_PERSISTENCIA_EVENTO,
            provider_called=observation.provider_called,
        )
    return GeracaoExtracao(response=observation.response, evento_id=observation.evento_id)


def _marcador_da_entrega(digest: str) -> str:
    return f"{_MARCADOR_ENTREGA}{digest}"


def _reverter_sessao_do_abatimento() -> None:
    """Desfaz o débito ainda não confirmado. A falha do rollback não substitui a pendência."""
    from app.extensions import db

    try:
        db.session.rollback()
    except Exception as exc:
        logger.warning(
            "Agente Compara extracao rollback do abatimento indisponivel tipo=%s",
            exc.__class__.__name__,
        )


def abater_entrega_confirmada(evento_id: int, digest: str) -> str:
    """Abate o consumo observado uma vez. Entrega já faturada não debita de novo.

    A dispensa ``franquia_reservada_sistema`` conclui a entrega sem abatimento.
    As demais ausências de débito continuam consumo incerto.
    Falha ao gravar o abatimento não confirma o débito e segue como consumo incerto.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.extensions import db
    from app.models import IaConsumoAbatimento, IaConsumoEvento
    from app.services.cleiton_franquia_operacional_service import (
        _iniciar_transacao_exclusiva,
        aplicar_motor_apos_ia_consumo_evento,
        stmt_lock_franquia,
    )

    try:
        identificador = int(evento_id)
    except (TypeError, ValueError) as exc:
        raise ExtracaoConsumoIncerto("evento_ausente") from exc
    try:
        marcador = _marcador_da_entrega(digest)
        _iniciar_transacao_exclusiva()
        evento = (
            db.session.query(IaConsumoEvento)
            .filter(IaConsumoEvento.id == identificador)
            .one_or_none()
        )
        if evento is None or evento.franquia_id is None or evento.flow_type != FLOW_EXTRACAO_TABELA:
            db.session.rollback()
            raise ExtracaoConsumoIncerto("evento_ausente")
        franquia_id = int(evento.franquia_id)
        db.session.execute(
            stmt_lock_franquia(),
            {"franquia_id": franquia_id},
        ).scalar_one_or_none()
        anteriores = (
            db.session.query(IaConsumoEvento)
            .filter(
                IaConsumoEvento.franquia_id == franquia_id,
                IaConsumoEvento.flow_type == FLOW_EXTRACAO_TABELA,
                IaConsumoEvento.error_summary == marcador,
            )
            .all()
        )
        for anterior in anteriores:
            marca = (
                db.session.query(IaConsumoAbatimento)
                .filter(IaConsumoAbatimento.ia_consumo_evento_id == int(anterior.id))
                .one_or_none()
            )
            if marca is not None and Decimal(str(marca.creditos)) > 0:
                db.session.commit()
                return "ja_faturada"
        if evento.status != "success":
            db.session.rollback()
            raise ExtracaoConsumoIncerto("evento_sem_entrega")
        resultado = aplicar_motor_apos_ia_consumo_evento(identificador, commit=False)
        if resultado.abateu_franquia or resultado.motivo_nao_abateu == "ja_apropriado":
            if not evento.error_summary:
                evento.error_summary = marcador
            db.session.commit()
            return "ja_faturada" if resultado.motivo_nao_abateu == "ja_apropriado" else "debitada"
        if resultado.motivo_nao_abateu == "creditos_zero":
            db.session.commit()
            return "sem_debito"
        if resultado.motivo_nao_abateu == "franquia_reservada_sistema":
            db.session.commit()
            return "dispensada"
        db.session.rollback()
        raise ExtracaoConsumoIncerto(resultado.motivo_nao_abateu or "debito_nao_confirmado")
    except ExtracaoConsumoIncerto:
        raise
    except SQLAlchemyError as exc:
        _reverter_sessao_do_abatimento()
        logger.warning(
            "Agente Compara extracao pendencia financeira no abatimento tipo=%s",
            exc.__class__.__name__,
        )
        raise ExtracaoConsumoIncerto("debito_nao_confirmado") from exc
