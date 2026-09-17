"""
Taxonomia Multiuser F1–F7 (Fase 8).

Consolida eventos já persistidos. Não cria trilha nova nem credencial API.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventoMultiuserTaxonomia:
    evento: str
    fonte: str
    idempotency_correlation: str
    auditoria: str


EVENTOS_MULTIUSER_V1: tuple[EventoMultiuserTaxonomia, ...] = (
    EventoMultiuserTaxonomia(
        evento="contratacao_ciclo_aplicado",
        fonte="conta_multiuser_contratacao_service",
        idempotency_correlation="MonetizacaoFato.idempotency_key / correlation_key",
        auditoria="MonetizacaoFato tipo_fato=stripe_multiuser_ciclo_aplicado",
    ),
    EventoMultiuserTaxonomia(
        evento="convite_criado",
        fonte="conta_multiuser_convite_service",
        idempotency_correlation="ContaMultiuserConvite.id / token assinado",
        auditoria="log sanitizado evento=convite_criado",
    ),
    EventoMultiuserTaxonomia(
        evento="convite_enviado",
        fonte="conta_multiuser_convite_service",
        idempotency_correlation="ContaMultiuserConvite.id / enviado_em após send_email",
        auditoria="log sanitizado evento=convite_enviado",
    ),
    EventoMultiuserTaxonomia(
        evento="convite_aceito",
        fonte="conta_multiuser_convite_service",
        idempotency_correlation="ContaMultiuserConvite + CSRF de aceite",
        auditoria="AuditoriaGerencial tipo_decisao=multiuser_convite_transferencia + log convite_aceito",
    ),
    EventoMultiuserTaxonomia(
        evento="convite_expirado",
        fonte="conta_multiuser_capacidade_service",
        idempotency_correlation="ContaMultiuserConvite.id",
        auditoria="log sanitizado evento=convite_expirado",
    ),
    EventoMultiuserTaxonomia(
        evento="aumento_automatico_aprovado",
        fonte="conta_multiuser_aumento_service",
        idempotency_correlation="ContaMultiuserAumentoOperacao.idempotency_key / correlation_id",
        auditoria="MonetizacaoFato tipo_fato=multiuser_aumento_*",
    ),
    EventoMultiuserTaxonomia(
        evento="aumento_enviado_analise",
        fonte="conta_multiuser_aumento_service",
        idempotency_correlation="ContaMultiuserAumentoOperacao.correlation_id",
        auditoria="MonetizacaoFato tipo_fato=multiuser_aumento_enviado_analise",
    ),
    EventoMultiuserTaxonomia(
        evento="aumento_excepcional_decidido",
        fonte="conta_multiuser_aumento_excepcional_service",
        idempotency_correlation="decisao_idempotency_key / correlation_id",
        auditoria="AuditoriaGerencial tipo_decisao=multiuser_aumento_excepcional + MonetizacaoFato",
    ),
    EventoMultiuserTaxonomia(
        evento="pagamento_extraordinario_confirmado",
        fonte="conta_multiuser_cobranca_extraordinaria_service",
        idempotency_correlation="stripe event id / correlation_id da excepcional",
        auditoria="MonetizacaoFato tipo_fato=multiuser_excepcional_pagamento_confirmado",
    ),
    EventoMultiuserTaxonomia(
        evento="membership_revogado",
        fonte="conta_multiuser_revogacao_service",
        idempotency_correlation="MonetizacaoFato.idempotency_key",
        auditoria="MonetizacaoFato tipo_fato=membership_revogado",
    ),
    EventoMultiuserTaxonomia(
        evento="reducao_quantity_solicitada",
        fonte="conta_multiuser_reducao_service",
        idempotency_correlation="ContaMultiuserReducaoQuantity.idempotency_key / correlation_id",
        auditoria="MonetizacaoFato tipo_fato=reducao_quantity_*",
    ),
    EventoMultiuserTaxonomia(
        evento="titularidade_solicitada",
        fonte="conta_multiuser_titularidade_service",
        idempotency_correlation="ContaMultiuserTitularidadeSolicitacao.idempotency_key / correlation_id",
        auditoria="AuditoriaGerencial tipo_decisao=titularidade_*",
    ),
    EventoMultiuserTaxonomia(
        evento="divergencia_detectada",
        fonte="conta_multiuser_diagnostico_service",
        idempotency_correlation="correlation_id da operação pendente quando existir",
        auditoria="log sanitizado evento=multiuser_diagnostico + projeção admin",
    ),
)


def listar_eventos_multiuser_v1() -> tuple[EventoMultiuserTaxonomia, ...]:
    return EVENTOS_MULTIUSER_V1
