"""Entrega o link de conclusão que o lote anterior deixou pendente.

A entrada é a saída já reservada em aguardando_link_seguro. Não cria
outra resposta principal e não recupera segredo antigo: a conclusão
anterior pode ser substituída pela emissão do Lote 2, feita aqui,
com commit=False, no momento da entrega.

A emissão usa o onboarding_id da interpretação. O retorno traz a
conclusão criada, e é esse id que fica em EventoCanalSaida.conclusao_id.
Não há segunda busca por jornada ou por conclusão parecida.

O commit da conclusão e de preparando_link acontece antes do HTTP.
O texto com o link existe só na chamada ao adapter. Replay de
preparando_link não fala com a Meta de novo. Não há retry.
"""
from __future__ import annotations

import logging
import re
import threading

from flask import current_app

from app.extensions import db
from app.models import (
    EventoCanalRecebido,
    EventoCanalSaida,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    OnboardingCanal,
    OnboardingCanalConclusao,
    utcnow_naive,
)
from app.services.canal_aquisicao_service import (
    CODIGO_CONTA_EXISTENTE,
    PROVEDOR_WHATSAPP_META,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_ACEITAR_TERMOS,
    ACAO_EMITIR_SENHA,
    ACAO_ORIENTAR_CONTA,
    CODIGO_LINK_EMITIDO as CODIGO_INTERPRETACAO_LINK,
)
from app.services.canal_saida_whatsapp_service import ResultadoSaidaCanal
from app.services.canal_tentativa_envio_service import espelhar_tentativa_persistida
from app.services.onboarding_canal_conclusao_service import (
    CODIGO_LINK_EMITIDO,
    _hash_token,
    emitir_link_conclusao_onboarding,
    emitir_link_vinculo_conta_existente,
)
from app.services.whatsapp_meta_cloud_api_adapter import WhatsAppMetaCloudApiAdapter
from app.services.whatsapp_meta_config import configuracao_envio

logger = logging.getLogger(__name__)

CODIGO_SAIDA_AUSENTE = "saida_ausente"
CODIGO_SAIDA_INVALIDA = "saida_invalida"
CODIGO_FINALIDADE_INDISPONIVEL = "finalidade_indisponivel"
CODIGO_SEGREDO_AUSENTE = "segredo_ausente"
CODIGO_JORNADA_AUSENTE = "jornada_ausente"
CODIGO_JORNADA_INELEGIVEL = "jornada_inelegivel"
CODIGO_CONCLUSAO_DIVERGENTE = "conclusao_divergente"

_FRASE_DEFINIR_SENHA = (
    "Seu cadastro está quase pronto. Por segurança, crie sua senha neste link: "
)
_FRASE_VINCULAR_CONTA = (
    "Este e-mail já tem conta. Entre e confirme a conexão neste link: "
)
_DIGITOS = re.compile(r"^[0-9]{1,32}$")

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


def _trava(saida_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(saida_id)
        if lock is None:
            lock = threading.Lock()
            _travas[saida_id] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoSaidaCanal) -> None:
    logger.info(
        "canal_saida_link saida_id=%s evento_id=%s correlation_id=%s "
        "status=%s codigo=%s conclusao_id=%s",
        resultado.saida_id if resultado.saida_id is not None else "-",
        resultado.evento_entrada_id if resultado.evento_entrada_id is not None else "-",
        resultado.correlation_id or "-",
        resultado.status_envio or "-",
        resultado.codigo_erro or resultado.codigo,
        resultado.conclusao_id if resultado.conclusao_id is not None else "-",
    )


def _resultado(
    codigo: str,
    *,
    status_envio: str | None = None,
    provider_message_id: str | None = None,
    codigo_erro: str | None = None,
    correlation_id: str | None = None,
    saida_id: int | None = None,
    evento_entrada_id: int | None = None,
    conclusao_id: int | None = None,
) -> ResultadoSaidaCanal:
    return ResultadoSaidaCanal(
        codigo=codigo,
        status_envio=status_envio,
        provider_message_id=provider_message_id,
        codigo_erro=codigo_erro,
        correlation_id=correlation_id,
        saida_id=saida_id,
        evento_entrada_id=evento_entrada_id,
        conclusao_id=conclusao_id,
    )


def _de_linha(row: EventoCanalSaida) -> ResultadoSaidaCanal:
    codigo = (
        row.codigo_erro
        if row.status_envio == EventoCanalSaida.STATUS_ERRO
        else row.status_envio
    )
    conclusao_id = int(row.conclusao_id) if row.conclusao_id is not None else None
    return _resultado(
        codigo or row.status_envio,
        status_envio=row.status_envio,
        provider_message_id=row.provider_message_id,
        codigo_erro=row.codigo_erro,
        correlation_id=row.correlation_id,
        saida_id=int(row.id),
        evento_entrada_id=int(row.evento_entrada_id),
        conclusao_id=conclusao_id,
    )


def _recarregar(saida_id: int, codigo: str | None = None) -> ResultadoSaidaCanal:
    db.session.rollback()
    atual = db.session.get(EventoCanalSaida, saida_id)
    if atual is None:
        return _resultado(codigo or CODIGO_SAIDA_AUSENTE, saida_id=saida_id)
    if codigo is None:
        return _de_linha(atual)
    conclusao_id = int(atual.conclusao_id) if atual.conclusao_id is not None else None
    return _resultado(
        codigo,
        status_envio=atual.status_envio,
        provider_message_id=atual.provider_message_id,
        codigo_erro=atual.codigo_erro,
        correlation_id=atual.correlation_id,
        saida_id=int(atual.id),
        evento_entrada_id=int(atual.evento_entrada_id),
        conclusao_id=conclusao_id,
    )


def _finalidade(interpretacao: InterpretacaoConversacionalCanal) -> str | None:
    etapa = interpretacao.etapa_final
    if (
        interpretacao.acao == ACAO_ORIENTAR_CONTA
        and interpretacao.codigo == CODIGO_CONTA_EXISTENTE
        and etapa in {None, OnboardingCanal.ETAPA_EMAIL}
    ):
        return OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    if (
        interpretacao.acao in {ACAO_EMITIR_SENHA, ACAO_ACEITAR_TERMOS}
        and interpretacao.codigo == CODIGO_INTERPRETACAO_LINK
        and etapa in {None, OnboardingCanal.ETAPA_SENHA}
    ):
        return OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    return None


def _identidade_do_evento(evento: EventoCanalRecebido) -> IdentidadeCanalExterna | None:
    sujeito = evento.sujeito_externo
    if not isinstance(sujeito, str) or not sujeito:
        return None
    linhas = (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            sujeito_externo=sujeito,
        )
        .filter(IdentidadeCanalExterna.estado != IdentidadeCanalExterna.ESTADO_REVOGADA)
        .all()
    )
    if len(linhas) != 1:
        return None
    return linhas[0]


def _finalidade_compativel(jornada: OnboardingCanal, finalidade: str) -> bool:
    if finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA:
        return jornada.etapa == OnboardingCanal.ETAPA_SENHA
    if finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA:
        return jornada.etapa == OnboardingCanal.ETAPA_EMAIL
    return False


def _jornada_alvo(
    interpretacao: InterpretacaoConversacionalCanal,
    evento: EventoCanalRecebido,
    finalidade: str,
) -> tuple[OnboardingCanal | None, str | None]:
    if not _id_valido(interpretacao.onboarding_id):
        return None, CODIGO_JORNADA_AUSENTE
    jornada = db.session.get(OnboardingCanal, int(interpretacao.onboarding_id))
    if jornada is None or int(jornada.id) != int(interpretacao.onboarding_id):
        return None, CODIGO_JORNADA_AUSENTE
    identidade = _identidade_do_evento(evento)
    if identidade is None or int(jornada.identidade_id) != int(identidade.id):
        return None, CODIGO_JORNADA_AUSENTE
    if not _finalidade_compativel(jornada, finalidade):
        return None, CODIGO_JORNADA_INELEGIVEL
    return jornada, None


def _secret() -> str | None:
    try:
        chave = current_app.config.get("SECRET_KEY")
    except RuntimeError:
        return None
    if not isinstance(chave, str) or not chave.strip():
        return None
    return chave


def _url_conclusao(segredo: str) -> str:
    return f"/onboarding/canal/concluir/{segredo}"


def _emitir(identidade_id: int, finalidade: str, secret: str, onboarding_id: int):
    comum = {
        "secret_key": secret,
        "build_url": _url_conclusao,
        "commit": False,
        "onboarding_id": onboarding_id,
    }
    if finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA:
        return emitir_link_conclusao_onboarding(identidade_id, **comum)
    return emitir_link_vinculo_conta_existente(identidade_id, **comum)


def _hash_confere(token: str, row: OnboardingCanalConclusao) -> bool:
    return _hash_token(token) == row.token_hash


def _mesma_conclusao(
    saida: EventoCanalSaida,
    conclusao_id: int,
    onboarding_id: int,
    finalidade: str,
    token: str,
) -> bool:
    db.session.refresh(saida)
    if saida.conclusao_id is None or int(saida.conclusao_id) != conclusao_id:
        return False
    criada = db.session.get(OnboardingCanalConclusao, conclusao_id)
    if criada is None:
        return False
    db.session.refresh(criada)
    if int(criada.onboarding_id) != onboarding_id:
        return False
    if criada.finalidade != finalidade or criada.estado != OnboardingCanalConclusao.ESTADO_EMITIDO:
        return False
    return _hash_confere(token, criada)


def _postar(
    finalidade: str,
    link: str,
    *,
    phone_number_id: str,
    destinatario: str,
):
    frase = (
        _FRASE_DEFINIR_SENHA
        if finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
        else _FRASE_VINCULAR_CONTA
    )
    return WhatsAppMetaCloudApiAdapter().enviar_texto(
        phone_number_id=phone_number_id,
        destinatario=destinatario,
        texto=frase + link,
    )


def _reivindicar(saida_id: int) -> int:
    # O UPDATE condicional é o CAS. No SQLite ele segura a escrita até
    # o commit, então a emissão seguinte entra na mesma transação.
    alteradas = EventoCanalSaida.query.filter_by(
        id=saida_id,
        status_envio=EventoCanalSaida.STATUS_AGUARDANDO_LINK,
    ).update(
        {"status_envio": EventoCanalSaida.STATUS_PREPARANDO_LINK},
        synchronize_session=False,
    )
    if alteradas == 1:
        espelhar_tentativa_persistida(saida_id)
    return alteradas


def _associar(saida_id: int, conclusao_id: int) -> int:
    alteradas = EventoCanalSaida.query.filter_by(
        id=saida_id,
        status_envio=EventoCanalSaida.STATUS_PREPARANDO_LINK,
    ).update({"conclusao_id": conclusao_id}, synchronize_session=False)
    if alteradas == 1:
        espelhar_tentativa_persistida(saida_id)
    return alteradas


def _concluir_envio(
    saida_id: int,
    *,
    aceito: bool,
    provider_message_id: str | None,
    codigo_erro: str | None,
) -> EventoCanalSaida | None:
    if aceito:
        valores = {
            "status_envio": EventoCanalSaida.STATUS_ACEITO,
            "provider_message_id": provider_message_id,
            "codigo_erro": None,
            "enviado_em": utcnow_naive(),
        }
    else:
        valores = {
            "status_envio": EventoCanalSaida.STATUS_ERRO,
            "provider_message_id": None,
            "codigo_erro": codigo_erro or EventoCanalSaida.CODIGO_RESPOSTA_INVALIDA,
            "enviado_em": None,
        }
    alteradas = EventoCanalSaida.query.filter_by(
        id=saida_id,
        status_envio=EventoCanalSaida.STATUS_PREPARANDO_LINK,
    ).update(valores, synchronize_session=False)
    if alteradas == 1:
        espelhar_tentativa_persistida(saida_id)
    db.session.commit()
    return db.session.get(EventoCanalSaida, saida_id)


def _entregar(saida_id: int) -> ResultadoSaidaCanal:
    row = db.session.get(EventoCanalSaida, saida_id)
    if row is None:
        return _resultado(CODIGO_SAIDA_AUSENTE, saida_id=saida_id)
    if row.status_envio != EventoCanalSaida.STATUS_AGUARDANDO_LINK:
        return _de_linha(row)
    if configuracao_envio() is None:
        return _resultado(
            EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE,
            status_envio=row.status_envio,
            codigo_erro=EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE,
            correlation_id=row.correlation_id,
            saida_id=int(row.id),
            evento_entrada_id=int(row.evento_entrada_id),
        )
    if row.interpretacao_id is None:
        return _de_linha(row)
    interpretacao = db.session.get(
        InterpretacaoConversacionalCanal,
        int(row.interpretacao_id),
    )
    if interpretacao is None:
        return _de_linha(row)
    evento = db.session.get(EventoCanalRecebido, int(row.evento_entrada_id))
    if evento is None:
        return _de_linha(row)
    phone_number_id = evento.contexto_destino
    destinatario = evento.sujeito_externo
    if (
        not isinstance(phone_number_id, str)
        or not _DIGITOS.fullmatch(phone_number_id)
        or not isinstance(destinatario, str)
        or not _DIGITOS.fullmatch(destinatario)
    ):
        return _de_linha(row)
    finalidade = _finalidade(interpretacao)
    if finalidade is None:
        return _resultado(
            CODIGO_FINALIDADE_INDISPONIVEL,
            status_envio=row.status_envio,
            correlation_id=row.correlation_id,
            saida_id=int(row.id),
            evento_entrada_id=int(row.evento_entrada_id),
        )
    jornada, codigo_jornada = _jornada_alvo(interpretacao, evento, finalidade)
    if jornada is None:
        return _resultado(
            codigo_jornada or CODIGO_JORNADA_AUSENTE,
            status_envio=row.status_envio,
            correlation_id=row.correlation_id,
            saida_id=int(row.id),
            evento_entrada_id=int(row.evento_entrada_id),
        )
    onboarding_id = int(jornada.id)
    secret = _secret()
    if secret is None:
        return _resultado(
            CODIGO_SEGREDO_AUSENTE,
            status_envio=row.status_envio,
            correlation_id=row.correlation_id,
            saida_id=int(row.id),
            evento_entrada_id=int(row.evento_entrada_id),
        )
    identidade_id = int(jornada.identidade_id)
    if _reivindicar(saida_id) != 1:
        return _recarregar(saida_id)
    emissao = _emitir(identidade_id, finalidade, secret, onboarding_id)
    if (
        emissao.codigo != CODIGO_LINK_EMITIDO
        or not emissao.persistiu
        or not isinstance(emissao.url, str)
        or not emissao.url
        or not isinstance(emissao.token, str)
        or not emissao.token
        or not _id_valido(emissao.conclusao_id)
        or not _id_valido(emissao.onboarding_id)
        or emissao.finalidade != finalidade
    ):
        return _recarregar(saida_id, emissao.codigo)
    conclusao_id = int(emissao.conclusao_id)
    onboarding_emitido = int(emissao.onboarding_id)
    link = emissao.url
    token = emissao.token
    del emissao
    codigo_falha = None
    try:
        if (
            onboarding_emitido != int(interpretacao.onboarding_id)
            or token not in link
            or _associar(saida_id, conclusao_id) != 1
            or not _mesma_conclusao(
                row,
                conclusao_id,
                onboarding_emitido,
                finalidade,
                token,
            )
        ):
            codigo_falha = CODIGO_CONCLUSAO_DIVERGENTE
        else:
            db.session.commit()
    except Exception:
        codigo_falha = CODIGO_CONCLUSAO_DIVERGENTE
    finally:
        del token
    if codigo_falha is not None:
        del link
        return _recarregar(saida_id, codigo_falha)
    try:
        http = _postar(
            finalidade,
            link,
            phone_number_id=phone_number_id,
            destinatario=destinatario,
        )
    except Exception:
        logger.info(
            "canal_saida_link saida_id=%s status=%s codigo=%s conclusao_id=%s",
            saida_id,
            EventoCanalSaida.STATUS_PREPARANDO_LINK,
            "falha_envio",
            conclusao_id,
        )
        db.session.rollback()
        atual = db.session.get(EventoCanalSaida, saida_id)
        if atual is None:
            return _resultado(CODIGO_SAIDA_AUSENTE, saida_id=saida_id)
        return _de_linha(atual)
    finally:
        del link
    if http.aceito:
        status_aceito = True
        message_id = http.provider_message_id
        codigo_erro = None
    else:
        status_aceito = False
        message_id = None
        codigo_erro = http.codigo_erro or EventoCanalSaida.CODIGO_RESPOSTA_INVALIDA
    try:
        marcada = _concluir_envio(
            saida_id,
            aceito=status_aceito,
            provider_message_id=message_id,
            codigo_erro=codigo_erro,
        )
    except Exception:
        db.session.rollback()
        marcada = db.session.get(EventoCanalSaida, saida_id)
    if marcada is None:
        return _resultado(
            EventoCanalSaida.STATUS_PREPARANDO_LINK,
            status_envio=EventoCanalSaida.STATUS_PREPARANDO_LINK,
            saida_id=saida_id,
            conclusao_id=conclusao_id,
        )
    return _de_linha(marcada)


def entregar_link_seguro(saida_id: int) -> ResultadoSaidaCanal:
    """Entrega o link da saída pendente, ou devolve o estado já gravado."""
    if not _id_valido(saida_id):
        resultado = _resultado(CODIGO_SAIDA_INVALIDA)
        _log(resultado)
        return resultado
    with _trava(int(saida_id)):
        resultado = _entregar(int(saida_id))
        _log(resultado)
        return resultado
