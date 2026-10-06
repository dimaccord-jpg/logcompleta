"""Elegibilidade de texto livre antes do transporte WhatsApp.

Confirmação de quem envia não é opt-in de quem recebe.
Sem janela ou consentimento conhecido, terceiro não recebe texto livre.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.models import EventoCanalRecebido, IdentidadeCanalExterna, utcnow_naive
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_telefone_declarado_service import digitos_e164

JANELA_ATENDIMENTO = timedelta(hours=24)

CODIGO_PROPRIO_VINCULADO = "proprio_vinculado"
CODIGO_JANELA_ABERTA = "janela_aberta"
CODIGO_SEM_JANELA = "sem_janela"
CODIGO_SEM_CONSENTIMENTO = "sem_consentimento"
CODIGO_CONSENTIMENTO_REVOGADO = "consentimento_revogado"
CODIGO_EVIDENCIA_INSUFICIENTE = "evidencia_insuficiente"
CODIGO_SEM_REMETENTE = "sem_remetente"

MENSAGEM_TERCEIRO_BLOQUEADO = (
    "O destinatário precisa iniciar uma conversa com o AgenteFrete no WhatsApp "
    "para eu poder enviar esta resposta."
)


@dataclass(frozen=True)
class ElegibilidadeWhatsApp:
    elegivel: bool
    codigo: str
    phone_number_id: str | None = None


def _so_digitos(valor: object) -> str | None:
    if not isinstance(valor, str) or not valor or len(valor) > 32:
        return None
    if not all(caractere.isdigit() for caractere in valor):
        return None
    return valor


def elegibilidade_proprio(*, vinculo_valido: bool) -> ElegibilidadeWhatsApp:
    if vinculo_valido:
        return ElegibilidadeWhatsApp(elegivel=True, codigo=CODIGO_PROPRIO_VINCULADO)
    return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_EVIDENCIA_INSUFICIENTE)


def elegibilidade_terceiro(telefone_e164: str) -> ElegibilidadeWhatsApp:
    digitos = digitos_e164(telefone_e164)
    if digitos is None:
        return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_EVIDENCIA_INSUFICIENTE)
    linhas = IdentidadeCanalExterna.query.filter_by(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=digitos,
    ).all()
    if not linhas:
        return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_SEM_CONSENTIMENTO)
    ativas = [
        linha
        for linha in linhas
        if linha.estado != IdentidadeCanalExterna.ESTADO_REVOGADA and linha.revogada_em is None
    ]
    if not ativas:
        return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_CONSENTIMENTO_REVOGADO)
    remetentes: list[str] = []
    for linha in ativas:
        remetente = _so_digitos(linha.contexto_destino)
        if remetente is None:
            if len(ativas) == 1:
                return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_SEM_REMETENTE)
            return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_EVIDENCIA_INSUFICIENTE)
        remetentes.append(remetente)
    distintos = set(remetentes)
    if len(distintos) != 1:
        return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_EVIDENCIA_INSUFICIENTE)
    phone_number_id = next(iter(distintos))
    limite = utcnow_naive() - JANELA_ATENDIMENTO
    recente = (
        EventoCanalRecebido.query.filter_by(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
            sujeito_externo=digitos,
            contexto_destino=phone_number_id,
        )
        .filter(EventoCanalRecebido.recebido_em >= limite)
        .order_by(EventoCanalRecebido.id.desc())
        .first()
    )
    if recente is None:
        return ElegibilidadeWhatsApp(elegivel=False, codigo=CODIGO_SEM_JANELA)
    return ElegibilidadeWhatsApp(
        elegivel=True,
        codigo=CODIGO_JANELA_ABERTA,
        phone_number_id=phone_number_id,
    )
