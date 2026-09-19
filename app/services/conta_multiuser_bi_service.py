"""
Projeção administrativa Multiuser (Fase 8).

Somente metadados comerciais/organizacionais. Sem chats, documentos,
uploads, nomes de arquivo operacional ou resultados privados.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import (
    Conta,
    ContaMultiuserAumentoExcepcional,
    ContaMultiuserAumentoOperacao,
    ContaMultiuserConvite,
    ContaMultiuserReducaoQuantity,
    ContaMultiuserTitularidadeSolicitacao,
    ContaVinculoOrganizacional,
    MonetizacaoFato,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    SLUG_CONTA_SISTEMA,
    STATUS_DIAG_DIVERGENTE,
    STATUS_DIAG_INCONCLUSIVO,
    STATUS_DIAG_RECONCILIACAO_NECESSARIA,
)


@dataclass
class ContaDivergenteAdmin:
    conta_id: int
    conta_nome: str
    status: str
    tipos: str
    detectado_em: str
    correlation_id: str | None = None


@dataclass
class SaudeMultiuserAdmin:
    contas_ativas: int = 0
    quantidade_contratada_total: int = 0
    memberships_ativos: int = 0
    convites_pendentes: int = 0
    reducoes_pendentes: int = 0
    reducoes_bloqueadas: int = 0
    aumentos_excepcionais_em_analise: int = 0
    pagamentos_extraordinarios_pendentes: int = 0
    operacoes_reconciliacao_necessaria: int = 0
    revogacoes: int = 0
    transferencias_titularidade: int = 0
    divergencias_detectadas: int = 0
    contas_divergentes: list[ContaDivergenteAdmin] = field(default_factory=list)

    def para_template(self) -> dict[str, Any]:
        return {
            "contas_ativas": self.contas_ativas,
            "quantidade_contratada_total": self.quantidade_contratada_total,
            "memberships_ativos": self.memberships_ativos,
            "convites_pendentes": self.convites_pendentes,
            "reducoes_pendentes": self.reducoes_pendentes,
            "reducoes_bloqueadas": self.reducoes_bloqueadas,
            "aumentos_excepcionais_em_analise": self.aumentos_excepcionais_em_analise,
            "pagamentos_extraordinarios_pendentes": self.pagamentos_extraordinarios_pendentes,
            "operacoes_reconciliacao_necessaria": self.operacoes_reconciliacao_necessaria,
            "revogacoes": self.revogacoes,
            "transferencias_titularidade": self.transferencias_titularidade,
            "divergencias_detectadas": self.divergencias_detectadas,
            "contas_divergentes": self.contas_divergentes,
        }


_CAMPOS_PROIBIDOS_BI = frozenset(
    {
        "chat",
        "documento",
        "upload",
        "arquivo",
        "prepared_context",
        "audit_batch",
        "tabela",
        "comparison_calculation",
    }
)


def _contas_multiuser_q() -> Any:
    return Conta.query.filter(Conta.multiuser_ativa.is_(True)).filter(
        Conta.slug != SLUG_CONTA_SISTEMA
    )


def projetar_saude_multiuser_admin() -> SaudeMultiuserAdmin:
    contas = _contas_multiuser_q().all()
    saude = SaudeMultiuserAdmin(contas_ativas=len(contas))
    saude.quantidade_contratada_total = sum(
        int(c.quantidade_assentos_contratados or 0) for c in contas
    )
    ids = [int(c.id) for c in contas]
    if not ids:
        return saude

    saude.memberships_ativos = ContaVinculoOrganizacional.query.filter(
        ContaVinculoOrganizacional.conta_id.in_(ids),
        ContaVinculoOrganizacional.estado == ESTADO_ATIVO,
    ).count()
    saude.convites_pendentes = ContaMultiuserConvite.query.filter(
        ContaMultiuserConvite.conta_id.in_(ids),
        ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
    ).count()
    saude.reducoes_pendentes = ContaMultiuserReducaoQuantity.query.filter(
        ContaMultiuserReducaoQuantity.conta_id.in_(ids),
        ContaMultiuserReducaoQuantity.estado == ContaMultiuserReducaoQuantity.ESTADO_PENDENTE,
    ).count()
    saude.reducoes_bloqueadas = ContaMultiuserReducaoQuantity.query.filter(
        ContaMultiuserReducaoQuantity.conta_id.in_(ids),
        ContaMultiuserReducaoQuantity.estado
        == ContaMultiuserReducaoQuantity.ESTADO_BLOQUEADA_NO_CORTE,
    ).count()
    saude.aumentos_excepcionais_em_analise = ContaMultiuserAumentoExcepcional.query.filter(
        ContaMultiuserAumentoExcepcional.conta_id.in_(ids),
        ContaMultiuserAumentoExcepcional.estado
        == ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE,
    ).count()
    saude.pagamentos_extraordinarios_pendentes = ContaMultiuserAumentoExcepcional.query.filter(
        ContaMultiuserAumentoExcepcional.conta_id.in_(ids),
        ContaMultiuserAumentoExcepcional.estado.in_(
            (
                ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO,
                ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
            )
        ),
    ).count()
    f6_rec = ContaMultiuserAumentoExcepcional.query.filter(
        ContaMultiuserAumentoExcepcional.conta_id.in_(ids),
        ContaMultiuserAumentoExcepcional.estado
        == ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA,
    ).count()
    f5_rec = ContaMultiuserAumentoOperacao.query.filter(
        ContaMultiuserAumentoOperacao.conta_id.in_(ids),
        ContaMultiuserAumentoOperacao.estado
        == ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO,
    ).count()
    saude.operacoes_reconciliacao_necessaria = int(f6_rec) + int(f5_rec)
    saude.revogacoes = MonetizacaoFato.query.filter(
        MonetizacaoFato.tipo_fato == "membership_revogado",
        MonetizacaoFato.conta_id.in_(ids),
    ).count()
    saude.transferencias_titularidade = ContaMultiuserTitularidadeSolicitacao.query.filter(
        ContaMultiuserTitularidadeSolicitacao.conta_id.in_(ids),
        ContaMultiuserTitularidadeSolicitacao.estado
        == ContaMultiuserTitularidadeSolicitacao.ESTADO_APROVADA,
    ).count()

    nomes = {int(c.id): (c.nome or f"Conta {c.id}") for c in contas}
    from app.services.conta_multiuser_diagnostico_service import diagnosticar_conta_multiuser

    # Mesma autoridade do diagnóstico individual (leitura Stripe live).
    # consultar_stripe=False mascarava divergência real presente só na leitura.
    for conta in contas:
        diag = diagnosticar_conta_multiuser(int(conta.id), consultar_stripe=True)
        if diag.status in (
            STATUS_DIAG_DIVERGENTE,
            STATUS_DIAG_RECONCILIACAO_NECESSARIA,
            STATUS_DIAG_INCONCLUSIVO,
        ):
            tipos = ",".join(sorted({a.codigo for a in diag.achados}))
            saude.contas_divergentes.append(
                ContaDivergenteAdmin(
                    conta_id=int(conta.id),
                    conta_nome=nomes.get(int(conta.id), f"Conta {conta.id}"),
                    status=diag.status,
                    tipos=tipos,
                    detectado_em=diag.detectado_em,
                    correlation_id=diag.correlation_id,
                )
            )
    saude.divergencias_detectadas = len(saude.contas_divergentes)
    return saude


def bi_expoe_conteudo_operacional(payload: dict[str, Any]) -> bool:
    texto = str(payload).lower()
    return any(token in texto for token in _CAMPOS_PROIBIDOS_BI)
