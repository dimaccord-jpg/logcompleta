"""
Interpreta um evento de canal já roteado para guest ou onboarding.

O texto vira ação determinística na jornada existente. A saída é só uma
resposta interna. Não envia mensagem ao provedor, não chama modelo e não
consulta cobrança.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ConteudoTextualCanal,
    EventoCanalRecebido,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    OnboardingCanal,
    OnboardingCanalConclusao,
)
from app.services.canal_aquisicao_service import (
    CODIGO_CONTA_EXISTENTE,
    CODIGO_TRANSICAO_CONFLITO,
    CanalAquisicaoError,
    EstadoOnboardingCanal,
    iniciar_onboarding_canal,
    obter_proxima_etapa,
    registrar_resposta_onboarding,
)
from app.services.canal_entrada_processamento_service import (
    ROTA_GUEST,
    ROTA_ONBOARDING,
    RecusaConteudo,
    validar_texto_operacional,
)
from app.services.onboarding_canal_conclusao_service import (
    CODIGO_LINK_EMITIDO,
    emitir_link_conclusao_onboarding,
    emitir_link_vinculo_conta_existente,
)
from app.services.onboarding_entrevista_definicao import (
    JOB_ROLES,
    proxima_pergunta,
)

logger = logging.getLogger(__name__)

CODIGO_ONBOARDING_INICIADO = "onboarding_iniciado"
CODIGO_RESPOSTA_REGISTRADA = "resposta_registrada"
CODIGO_ORIENTACAO_GUEST = "orientacao_guest"
CODIGO_NOME_INVALIDO = "nome_invalido"
CODIGO_EMAIL_INVALIDO = "email_invalido"
CODIGO_CARGO_INVALIDO = "cargo_invalido"
CODIGO_RESPOSTA_INVALIDA = "resposta_invalida"
CODIGO_TERMOS_APRESENTADOS = "termos_apresentados"
CODIGO_ACEITE_NAO_RECONHECIDO = "aceite_nao_reconhecido"
CODIGO_LINK_NAO_EMITIDO = "link_nao_emitido"
CODIGO_ROTA_NAO_OPERADA = "rota_nao_operada"
CODIGO_EVENTO_JA_TRATADO = "evento_ja_tratado"
CODIGO_EVENTO_EM_TRATAMENTO = "evento_em_tratamento"
CODIGO_EVENTO_NAO_ROTEADO = "evento_nao_roteado"
CODIGO_EVENTO_AUSENTE = "evento_ausente"
CODIGO_EVENTO_INVALIDO = "evento_invalido"
CODIGO_ROTEAMENTO_INDISPONIVEL = "roteamento_indisponivel"
CODIGO_TEXTO_INDISPONIVEL = "texto_indisponivel"
CODIGO_JORNADA_INDISPONIVEL = "jornada_indisponivel"
CODIGO_ERRO_SEGURO = "erro_seguro"
CODIGO_EM_TRATAMENTO = "em_tratamento"

ACAO_INICIAR = "iniciar_onboarding"
ACAO_ORIENTAR_GUEST = "orientar_guest"
ACAO_REGISTRAR_NOME = "registrar_nome"
ACAO_REJEITAR_NOME = "rejeitar_nome"
ACAO_REGISTRAR_EMAIL = "registrar_email"
ACAO_REJEITAR_EMAIL = "rejeitar_email"
ACAO_ORIENTAR_CONTA = "orientar_conta_existente"
ACAO_REGISTRAR_CARGO = "registrar_cargo"
ACAO_REJEITAR_CARGO = "rejeitar_cargo"
ACAO_REGISTRAR_ENTREVISTA = "registrar_entrevista"
ACAO_REJEITAR_ENTREVISTA = "rejeitar_entrevista"
ACAO_APRESENTAR_TERMOS = "apresentar_termos"
ACAO_ACEITAR_TERMOS = "aceitar_termos"
ACAO_REJEITAR_ACEITE = "rejeitar_aceite"
ACAO_EMITIR_SENHA = "emitir_link_senha"
ACAO_IGNORAR_ROTA = "ignorar_rota"
ACAO_RESERVADA = "reservado"
ACAO_ERRO = "erro_seguro"

TEXTO_GUEST = (
    "Recebi sua pergunta. Se quiser se cadastrar, envie uma nova mensagem."
)
TEXTO_NOME = "Qual é o seu nome?"
TEXTO_NOME_INVALIDO = "Não consegui usar esse nome. Envie seu nome para continuar."
TEXTO_EMAIL = "Qual é o seu e-mail?"
TEXTO_EMAIL_INVALIDO = "Esse e-mail não é válido. Envie um e-mail para continuar."
TEXTO_CARGO = "Qual é o seu cargo?"
TEXTO_CARGO_INVALIDO = "Não reconheci esse cargo. Escolha uma opção:"
TEXTO_ENTREVISTA_INVALIDA = "Não reconheci essa resposta. Escolha uma opção:"
TEXTO_TERMOS_RECUSA = (
    "O aceite precisa ser explícito. Responda ACEITO para continuar."
)
TEXTO_TERMOS_INDISPONIVEL = (
    "Não consegui abrir o Termo de Aceite agora. Envie uma nova mensagem para tentar de novo."
)
TEXTO_SENHA = (
    "Seu cadastro está quase pronto. Por segurança, crie sua senha no link enviado."
)
TEXTO_SENHA_JA_EMITIDO = (
    "Seu cadastro está quase pronto. O link para criar sua senha já foi enviado."
)
TEXTO_SENHA_INDISPONIVEL = (
    "Seu cadastro está quase pronto. Não foi possível gerar o link de senha agora."
)
TEXTO_CONTA = "Este e-mail já tem conta. Entre e confirme a conexão no link enviado."
TEXTO_CONTA_SEM_LINK = "Este e-mail já tem conta. Entre e confirme a conexão com esta conta."
_MARCA_LINK = "/onboarding/canal/concluir/"
TEXTO_EXPIRADA = "Esta jornada expirou."
TEXTO_ENCERRADA = "Esta jornada não está mais aberta."
TEXTO_CONVITE = "Para continuar o cadastro, envie uma nova mensagem."

_COMANDOS_INICIAR = frozenset(
    {
        "quero me cadastrar",
        "começar cadastro",
        "iniciar cadastro",
        "cadastrar",
    }
)
_COMANDO_ACEITE = "aceito"
_REFERENCIA_TERMO_LEGADA = "terms-v1"
_PREFIXO_REFERENCIA_TERMO = "terms-of-use:"
_CAMINHO_TERMOS = "/termos-de-uso"
_PROVEDOR_IDENTIDADE = "whatsapp_meta"
_ORIGEM = "whatsapp_meta"
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_ROTAS_OPERADAS = frozenset({ROTA_GUEST, ROTA_ONBOARDING})
_CODIGOS_INVALIDOS_ENTRADA = frozenset(
    {
        "nome_invalido",
        "email_invalido",
        "cargo_invalido",
        "resposta_invalida",
        "material_sensivel",
        "etapa_nao_aceita_campo",
    }
)

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()
_travas_identidade: dict[int, threading.Lock] = {}
_travas_identidade_guard = threading.Lock()


@dataclass(frozen=True)
class ResultadoInterpretacaoCanal:
    codigo: str
    acao: str | None = None
    texto_resposta: str | None = None
    identidade_id: int | None = None
    onboarding_id: int | None = None
    etapa_atual: str | None = None
    correlation_id: str | None = None
    conclusao_id: int | None = None


def _trava(evento_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(evento_id)
        if lock is None:
            lock = threading.Lock()
            _travas[evento_id] = lock
        return lock


def _trava_identidade(identidade_id: int) -> threading.Lock:
    """Serializa duas mensagens da mesma identidade até o commit da primeira."""
    with _travas_identidade_guard:
        lock = _travas_identidade.get(identidade_id)
        if lock is None:
            lock = threading.Lock()
            _travas_identidade[identidade_id] = lock
        return lock


def _id_valido(valor: object) -> bool:
    return not isinstance(valor, bool) and isinstance(valor, int) and valor > 0


def _log(resultado: ResultadoInterpretacaoCanal, evento_id: int | None = None) -> None:
    logger.info(
        "canal_interpretacao evento_id=%s identidade_id=%s onboarding_id=%s "
        "etapa=%s codigo=%s correlation_id=%s",
        evento_id if evento_id is not None else "-",
        resultado.identidade_id if resultado.identidade_id is not None else "-",
        resultado.onboarding_id if resultado.onboarding_id is not None else "-",
        resultado.etapa_atual or "-",
        resultado.codigo,
        resultado.correlation_id or "-",
    )


def _resultado(
    codigo: str,
    *,
    acao: str | None = None,
    texto_resposta: str | None = None,
    identidade_id: int | None = None,
    onboarding_id: int | None = None,
    etapa_atual: str | None = None,
    correlation_id: str | None = None,
    conclusao_id: int | None = None,
) -> ResultadoInterpretacaoCanal:
    return ResultadoInterpretacaoCanal(
        codigo=codigo,
        acao=acao,
        texto_resposta=texto_resposta,
        identidade_id=identidade_id,
        onboarding_id=onboarding_id,
        etapa_atual=etapa_atual,
        correlation_id=correlation_id,
        conclusao_id=conclusao_id,
    )


def normalizar_comando(texto: str) -> str:
    """Trim, casefold e um espaço entre palavras. Sem modelo."""
    return " ".join(texto.strip().casefold().split())


def _correlation_aceitavel(valor: str | None) -> str | None:
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if not _CORRELATION_RE.fullmatch(texto):
        return None
    return texto


def _linhas(pares: tuple[tuple[str, str], ...]) -> str:
    return "\n".join(f"{indice}. {rotulo}" for indice, (_chave, rotulo) in enumerate(pares, start=1))


def _texto_cargos(prefixo: str) -> str:
    return f"{prefixo}\n{_linhas(JOB_ROLES)}"


def _texto_pergunta(pergunta, prefixo: str | None = None) -> str:
    pares = tuple((opcao.key, opcao.label) for opcao in pergunta.opcoes)
    corpo = f"{pergunta.texto}\n{_linhas(pares)}"
    if prefixo:
        return f"{prefixo}\n{corpo}"
    return corpo


def _resolver_opcao(texto: str, pares: tuple[tuple[str, str], ...]) -> str | None:
    """Número 1..n ou label/chave já definidos. Sem aproximação."""
    normal = normalizar_comando(texto)
    if normal.isdigit() and not (len(normal) > 1 and normal.startswith("0")):
        indice = int(normal)
        if 1 <= indice <= len(pares):
            return pares[indice - 1][0]
        return None
    for chave, rotulo in pares:
        if normal == chave.casefold() or normal == normalizar_comando(rotulo):
            return chave
    return None


def _aceite_explicito(texto: str) -> bool:
    """Somente a palavra ACEITO, com caixa e espaços externos irrelevantes."""
    return normalizar_comando(texto) == _COMANDO_ACEITE


def _origem_publica() -> str | None:
    """Origem de PUBLIC_BASE_URL. Não usa host da request nem URL fixa."""
    bruto = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not bruto:
        try:
            from flask import current_app

            valor = current_app.config.get("PUBLIC_BASE_URL")
        except RuntimeError:
            valor = None
        if isinstance(valor, str):
            bruto = valor.strip()
    if not bruto or any(ch in bruto for ch in ("\r", "\n", "\x00", " ")):
        return None
    candidato = bruto.rstrip("/")
    try:
        parsed = urlparse(candidato)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment or "@" in parsed.netloc:
        return None
    if parsed.path not in ("", "/"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _url_publica_termos() -> str | None:
    origem = _origem_publica()
    if origem is None:
        return None
    return f"{origem}{_CAMINHO_TERMOS}"


def _texto_termos(url: str) -> str:
    return (
        "Leia o Termo de Aceite:\n"
        f"{url}\n"
        "\n"
        "Após a leitura, para continuar, responda ACEITO."
    )


def _texto_recusa_aceite() -> str:
    url = _url_publica_termos()
    if not url:
        return TEXTO_TERMOS_RECUSA
    return f"Leia o Termo de Aceite:\n{url}\n\n{TEXTO_TERMOS_RECUSA}"


def _referencia_termo_valida(referencia: str | None) -> bool:
    if not isinstance(referencia, str):
        return False
    texto = referencia.strip()
    if texto == _REFERENCIA_TERMO_LEGADA or not texto.startswith(_PREFIXO_REFERENCIA_TERMO):
        return False
    sufixo = texto[len(_PREFIXO_REFERENCIA_TERMO) :]
    if not sufixo.isdigit() or (len(sufixo) > 1 and sufixo.startswith("0")):
        return False
    return int(sufixo) > 0


def _termo_efetivamente_apresentado(estado: EstadoOnboardingCanal) -> bool:
    return bool(estado.termos_apresentados) and _referencia_termo_valida(estado.termos_referencia)


def _referencia_termo_ativo() -> str | None:
    from app.terms_services import get_active_term

    termo = get_active_term()
    if termo is None:
        return None
    termo_id = getattr(termo, "id", None)
    if isinstance(termo_id, bool) or not isinstance(termo_id, int) or termo_id <= 0:
        return None
    referencia = f"{_PREFIXO_REFERENCIA_TERMO}{termo_id}"
    if not _referencia_termo_valida(referencia):
        return None
    return referencia


def _texto_etapa_termos(estado: EstadoOnboardingCanal) -> str:
    if not _termo_efetivamente_apresentado(estado):
        return TEXTO_TERMOS_INDISPONIVEL
    url = _url_publica_termos()
    if url:
        return _texto_termos(url)
    return "Após a leitura, para continuar, responda ACEITO."


def _prompt_entrevista(estado: EstadoOnboardingCanal, prefixo: str | None = None) -> str:
    pergunta = proxima_pergunta(estado.job_role, estado.respostas_entrevista)
    if pergunta is None:
        return _texto_etapa_termos(estado)
    return _texto_pergunta(pergunta, prefixo)


def _prompt_etapa(estado: EstadoOnboardingCanal, prefixo: str | None = None) -> str:
    etapa = estado.etapa_atual
    if etapa == OnboardingCanal.ETAPA_CONVITE:
        return TEXTO_CONVITE
    if etapa == OnboardingCanal.ETAPA_NOME:
        return TEXTO_NOME if prefixo is None else f"{prefixo}\n{TEXTO_NOME}"
    if etapa == OnboardingCanal.ETAPA_EMAIL:
        return TEXTO_EMAIL if prefixo is None else f"{prefixo}\n{TEXTO_EMAIL}"
    if etapa == OnboardingCanal.ETAPA_CARGO:
        return _texto_cargos(prefixo or TEXTO_CARGO)
    if etapa == OnboardingCanal.ETAPA_ENTREVISTA:
        return _prompt_entrevista(estado, prefixo)
    if etapa == OnboardingCanal.ETAPA_TERMOS:
        texto = _texto_etapa_termos(estado)
        return texto if prefixo is None else f"{prefixo}\n{texto}"
    if etapa == OnboardingCanal.ETAPA_SENHA:
        return TEXTO_SENHA_JA_EMITIDO
    if etapa == OnboardingCanal.ETAPA_EXPIRADO:
        return TEXTO_EXPIRADA
    return TEXTO_ENCERRADA


def _de_estado(
    estado: EstadoOnboardingCanal,
    *,
    codigo: str,
    acao: str,
    texto: str,
    correlation_id: str | None,
    conclusao_id: int | None = None,
) -> ResultadoInterpretacaoCanal:
    return _resultado(
        codigo,
        acao=acao,
        texto_resposta=texto,
        identidade_id=int(estado.identidade_id),
        onboarding_id=int(estado.onboarding_id),
        etapa_atual=estado.etapa_atual,
        correlation_id=correlation_id,
        conclusao_id=conclusao_id,
    )


def _secret() -> str | None:
    try:
        from flask import current_app

        chave = current_app.config.get("SECRET_KEY")
    except RuntimeError:
        return None
    if not isinstance(chave, str) or not chave.strip():
        return None
    return chave


def _url_conclusao(token: str) -> str:
    return f"/onboarding/canal/concluir/{token}"


def _conclusao_emitida(
    onboarding_id: int,
    finalidade: str,
) -> OnboardingCanalConclusao | None:
    return (
        OnboardingCanalConclusao.query.filter_by(
            onboarding_id=onboarding_id,
            finalidade=finalidade,
            estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        )
        .order_by(OnboardingCanalConclusao.id.desc())
        .first()
    )


def _id_conclusao(row: OnboardingCanalConclusao | None) -> int | None:
    if row is None:
        return None
    return int(row.id)


def _texto_persistivel(texto: str | None, acao: str | None) -> str | None:
    """Resposta interna sem token, URL de conclusão nem segredo equivalente."""
    if texto is None:
        return None
    if _MARCA_LINK in texto.casefold():
        if acao == ACAO_ORIENTAR_CONTA:
            return TEXTO_CONTA
        if acao in {ACAO_EMITIR_SENHA, ACAO_ACEITAR_TERMOS}:
            return TEXTO_SENHA
        return None
    if len(texto) > InterpretacaoConversacionalCanal.TEXTO_MAXIMO:
        return None
    return texto


def _emitir_senha(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    finalidade = OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA
    onboarding_id = int(estado.onboarding_id)
    ja = _conclusao_emitida(onboarding_id, finalidade)
    if ja is not None:
        return _de_estado(
            estado,
            codigo=CODIGO_LINK_EMITIDO,
            acao=ACAO_EMITIR_SENHA,
            texto=TEXTO_SENHA_JA_EMITIDO,
            correlation_id=correlation_id,
            conclusao_id=_id_conclusao(ja),
        )
    secret = _secret()
    if secret is None:
        return _de_estado(
            estado,
            codigo=CODIGO_LINK_NAO_EMITIDO,
            acao=ACAO_EMITIR_SENHA,
            texto=TEXTO_SENHA_INDISPONIVEL,
            correlation_id=correlation_id,
        )
    emissao = emitir_link_conclusao_onboarding(
        int(estado.identidade_id),
        secret_key=secret,
        build_url=_url_conclusao,
        commit=False,
    )
    # O segredo fica só no retorno em memória do Lote 2. Não é copiado.
    if emissao.codigo != CODIGO_LINK_EMITIDO or not emissao.persistiu:
        return _de_estado(
            estado,
            codigo=CODIGO_LINK_NAO_EMITIDO,
            acao=ACAO_EMITIR_SENHA,
            texto=TEXTO_SENHA_JA_EMITIDO,
            correlation_id=correlation_id,
            conclusao_id=_id_conclusao(_conclusao_emitida(onboarding_id, finalidade)),
        )
    return _de_estado(
        estado,
        codigo=CODIGO_LINK_EMITIDO,
        acao=ACAO_EMITIR_SENHA,
        texto=TEXTO_SENHA,
        correlation_id=correlation_id,
        conclusao_id=_id_conclusao(_conclusao_emitida(onboarding_id, finalidade)),
    )


def _emitir_vinculo(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    finalidade = OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA
    onboarding_id = int(estado.onboarding_id)
    ja = _conclusao_emitida(onboarding_id, finalidade)
    if ja is not None:
        return _de_estado(
            estado,
            codigo=CODIGO_CONTA_EXISTENTE,
            acao=ACAO_ORIENTAR_CONTA,
            texto=TEXTO_CONTA_SEM_LINK,
            correlation_id=correlation_id,
            conclusao_id=_id_conclusao(ja),
        )
    secret = _secret()
    if secret is None:
        return _de_estado(
            estado,
            codigo=CODIGO_CONTA_EXISTENTE,
            acao=ACAO_ORIENTAR_CONTA,
            texto=TEXTO_CONTA_SEM_LINK,
            correlation_id=correlation_id,
        )
    emissao = emitir_link_vinculo_conta_existente(
        int(estado.identidade_id),
        secret_key=secret,
        build_url=_url_conclusao,
        commit=False,
    )
    if emissao.codigo != CODIGO_LINK_EMITIDO or not emissao.persistiu:
        return _de_estado(
            estado,
            codigo=CODIGO_CONTA_EXISTENTE,
            acao=ACAO_ORIENTAR_CONTA,
            texto=TEXTO_CONTA_SEM_LINK,
            correlation_id=correlation_id,
            conclusao_id=_id_conclusao(_conclusao_emitida(onboarding_id, finalidade)),
        )
    return _de_estado(
        estado,
        codigo=CODIGO_CONTA_EXISTENTE,
        acao=ACAO_ORIENTAR_CONTA,
        texto=TEXTO_CONTA,
        correlation_id=correlation_id,
        conclusao_id=_id_conclusao(_conclusao_emitida(onboarding_id, finalidade)),
    )


def _termos_indisponiveis(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    return _de_estado(
        estado,
        codigo=CODIGO_ERRO_SEGURO,
        acao=ACAO_APRESENTAR_TERMOS,
        texto=TEXTO_TERMOS_INDISPONIVEL,
        correlation_id=correlation_id,
    )


def _apresentar_termos(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    if _termo_efetivamente_apresentado(estado):
        return _de_estado(
            estado,
            codigo=CODIGO_TERMOS_APRESENTADOS,
            acao=ACAO_APRESENTAR_TERMOS,
            texto=_texto_etapa_termos(estado),
            correlation_id=correlation_id,
        )
    url = _url_publica_termos()
    referencia = _referencia_termo_ativo()
    if url is None or referencia is None:
        return _termos_indisponiveis(estado, correlation_id)
    try:
        estado = registrar_resposta_onboarding(
            int(estado.identidade_id),
            campo="apresentar_termos",
            termos_referencia=referencia,
            commit=False,
        )
    except CanalAquisicaoError:
        return _termos_indisponiveis(estado, correlation_id)
    if estado.codigo == CODIGO_TRANSICAO_CONFLITO:
        if _termo_efetivamente_apresentado(estado):
            return _de_estado(
                estado,
                codigo=CODIGO_TERMOS_APRESENTADOS,
                acao=ACAO_APRESENTAR_TERMOS,
                texto=_texto_termos(url),
                correlation_id=correlation_id,
            )
        return _termos_indisponiveis(estado, correlation_id)
    if not _termo_efetivamente_apresentado(estado) or estado.termos_referencia != referencia:
        return _termos_indisponiveis(estado, correlation_id)
    return _de_estado(
        estado,
        codigo=CODIGO_TERMOS_APRESENTADOS,
        acao=ACAO_APRESENTAR_TERMOS,
        texto=_texto_termos(url),
        correlation_id=correlation_id,
    )


def _apos_avanco(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
    *,
    acao: str,
) -> ResultadoInterpretacaoCanal:
    if estado.codigo == CODIGO_CONTA_EXISTENTE:
        return _emitir_vinculo(estado, correlation_id)
    if estado.codigo == CODIGO_TRANSICAO_CONFLITO:
        return _de_estado(
            estado,
            codigo=CODIGO_TRANSICAO_CONFLITO,
            acao=acao,
            texto=_prompt_etapa(estado),
            correlation_id=correlation_id,
        )
    if estado.etapa_atual == OnboardingCanal.ETAPA_TERMOS:
        return _apresentar_termos(estado, correlation_id)
    if estado.etapa_atual == OnboardingCanal.ETAPA_SENHA:
        return _emitir_senha(estado, correlation_id)
    return _de_estado(
        estado,
        codigo=CODIGO_RESPOSTA_REGISTRADA,
        acao=acao,
        texto=_prompt_etapa(estado),
        correlation_id=correlation_id,
    )


def _entrada_invalida(
    estado: EstadoOnboardingCanal,
    *,
    codigo: str,
    acao: str,
    texto: str,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    return _de_estado(
        estado,
        codigo=codigo,
        acao=acao,
        texto=texto,
        correlation_id=correlation_id,
    )


def _bloquear_identidade(identidade_id: int) -> IdentidadeCanalExterna | None:
    """Relê a identidade na decisão. No Postgres a linha fica travada até o commit."""
    return (
        IdentidadeCanalExterna.query.filter_by(id=identidade_id)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )


def _jornada_aberta(identidade_id: int) -> OnboardingCanal | None:
    return (
        OnboardingCanal.query.filter_by(identidade_id=identidade_id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )


def _orientacao_guest_concluida(
    evento: EventoCanalRecebido,
    identidade: IdentidadeCanalExterna,
) -> bool:
    """Histórico finalizado do mesmo provedor e sujeito. Ignora o evento atual."""
    sujeito = evento.sujeito_externo
    if identidade.estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        return False
    if identidade.provedor != _PROVEDOR_IDENTIDADE or identidade.sujeito_externo != sujeito:
        return False
    if evento.provider != EventoCanalRecebido.PROVIDER_META_WHATSAPP:
        return False
    if not isinstance(sujeito, str) or not sujeito:
        return False
    anterior = (
        db.session.query(InterpretacaoConversacionalCanal.id)
        .join(
            EventoCanalRecebido,
            InterpretacaoConversacionalCanal.evento_id == EventoCanalRecebido.id,
        )
        .filter(InterpretacaoConversacionalCanal.codigo == CODIGO_ORIENTACAO_GUEST)
        .filter(InterpretacaoConversacionalCanal.codigo != CODIGO_EM_TRATAMENTO)
        .filter(InterpretacaoConversacionalCanal.acao != ACAO_RESERVADA)
        .filter(EventoCanalRecebido.id != int(evento.id))
        .filter(EventoCanalRecebido.provider == evento.provider)
        .filter(EventoCanalRecebido.sujeito_externo == sujeito)
        .first()
    )
    return anterior is not None


def _pedir_nome(
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    return _de_estado(
        estado,
        codigo=CODIGO_ONBOARDING_INICIADO,
        acao=ACAO_INICIAR,
        texto=TEXTO_NOME,
        correlation_id=correlation_id,
    )


def _resposta_jornada_sem_consumir(
    identidade_id: int,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    """Mensagem guest que chegou com jornada já aberta não vira nome nem avança."""
    estado = obter_proxima_etapa(identidade_id, commit=False)
    if estado.etapa_atual == OnboardingCanal.ETAPA_NOME and not estado.nome:
        return _pedir_nome(estado, correlation_id)
    if estado.etapa_atual == OnboardingCanal.ETAPA_CONVITE:
        return _de_estado(
            estado,
            codigo=estado.codigo,
            acao=ACAO_INICIAR,
            texto=TEXTO_CONVITE,
            correlation_id=correlation_id,
        )
    return _de_estado(
        estado,
        codigo=estado.codigo,
        acao=ACAO_INICIAR,
        texto=_prompt_etapa(estado),
        correlation_id=correlation_id,
    )


def _abrir_coleta_nome(
    identidade_id: int,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    estado = iniciar_onboarding_canal(
        identidade_id,
        origem_aquisicao=_ORIGEM,
        correlation_id=_correlation_aceitavel(correlation_id),
        commit=False,
    )
    if estado.codigo == "jornada_expirada":
        return _de_estado(
            estado,
            codigo="jornada_expirada",
            acao=ACAO_INICIAR,
            texto=TEXTO_EXPIRADA,
            correlation_id=correlation_id,
        )
    if estado.etapa_atual == OnboardingCanal.ETAPA_CONVITE:
        try:
            estado = registrar_resposta_onboarding(
                identidade_id,
                campo="aceitar_convite",
                commit=False,
            )
        except CanalAquisicaoError:
            return _resposta_jornada_sem_consumir(identidade_id, correlation_id)
    if estado.codigo == CODIGO_TRANSICAO_CONFLITO or (
        estado.etapa_atual == OnboardingCanal.ETAPA_NOME and estado.nome
    ):
        return _resposta_jornada_sem_consumir(identidade_id, correlation_id)
    if estado.etapa_atual == OnboardingCanal.ETAPA_NOME and not estado.nome:
        return _pedir_nome(estado, correlation_id)
    return _de_estado(
        estado,
        codigo=estado.codigo,
        acao=ACAO_INICIAR,
        texto=_prompt_etapa(estado),
        correlation_id=correlation_id,
    )


def _guest(
    identidade_id: int,
    texto: str,
    correlation_id: str | None,
    evento: EventoCanalRecebido,
) -> ResultadoInterpretacaoCanal:
    identidade = _bloquear_identidade(identidade_id)
    if identidade is None or identidade.estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        return _resultado(
            CODIGO_JORNADA_INDISPONIVEL,
            acao=ACAO_ERRO,
            identidade_id=identidade_id,
            correlation_id=correlation_id,
        )
    aberta = _jornada_aberta(identidade_id)
    if aberta is not None and aberta.etapa != OnboardingCanal.ETAPA_CONVITE:
        return _resposta_jornada_sem_consumir(identidade_id, correlation_id)
    atalho = normalizar_comando(texto) in _COMANDOS_INICIAR
    if aberta is None and not atalho and not _orientacao_guest_concluida(evento, identidade):
        return _resultado(
            CODIGO_ORIENTACAO_GUEST,
            acao=ACAO_ORIENTAR_GUEST,
            texto_resposta=TEXTO_GUEST,
            identidade_id=identidade_id,
            correlation_id=correlation_id,
        )
    return _abrir_coleta_nome(identidade_id, correlation_id)


def _recusa_campo(
    exc: CanalAquisicaoError,
    estado: EstadoOnboardingCanal,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    etapa = estado.etapa_atual
    if exc.codigo in _CODIGOS_INVALIDOS_ENTRADA or etapa == OnboardingCanal.ETAPA_NOME:
        if etapa == OnboardingCanal.ETAPA_NOME:
            return _entrada_invalida(
                estado,
                codigo=CODIGO_NOME_INVALIDO,
                acao=ACAO_REJEITAR_NOME,
                texto=f"{TEXTO_NOME_INVALIDO}\n{TEXTO_NOME}",
                correlation_id=correlation_id,
            )
        if etapa == OnboardingCanal.ETAPA_EMAIL:
            return _entrada_invalida(
                estado,
                codigo=CODIGO_EMAIL_INVALIDO,
                acao=ACAO_REJEITAR_EMAIL,
                texto=f"{TEXTO_EMAIL_INVALIDO}\n{TEXTO_EMAIL}",
                correlation_id=correlation_id,
            )
    return _de_estado(
        estado,
        codigo=CODIGO_ERRO_SEGURO,
        acao=ACAO_ERRO,
        texto=_prompt_etapa(estado),
        correlation_id=correlation_id,
    )


def _onboarding(
    identidade_id: int,
    texto: str,
    correlation_id: str | None,
) -> ResultadoInterpretacaoCanal:
    estado = obter_proxima_etapa(identidade_id, commit=False)
    if estado.codigo == "jornada_expirada" or estado.etapa_atual == OnboardingCanal.ETAPA_EXPIRADO:
        return _de_estado(
            estado,
            codigo="jornada_expirada",
            acao=ACAO_ERRO,
            texto=TEXTO_EXPIRADA,
            correlation_id=correlation_id,
        )
    if estado.etapa_atual in OnboardingCanal.ETAPAS_TERMINAIS:
        return _de_estado(
            estado,
            codigo="jornada_encerrada",
            acao=ACAO_ERRO,
            texto=TEXTO_ENCERRADA,
            correlation_id=correlation_id,
        )
    etapa = estado.etapa_atual
    if etapa == OnboardingCanal.ETAPA_CONVITE:
        try:
            estado = registrar_resposta_onboarding(
                identidade_id,
                campo="aceitar_convite",
                commit=False,
            )
        except CanalAquisicaoError:
            return _resposta_jornada_sem_consumir(identidade_id, correlation_id)
        if estado.etapa_atual == OnboardingCanal.ETAPA_NOME and not estado.nome:
            return _pedir_nome(estado, correlation_id)
        return _apos_avanco(estado, correlation_id, acao=ACAO_INICIAR)
    if etapa == OnboardingCanal.ETAPA_NOME:
        try:
            estado = registrar_resposta_onboarding(
                identidade_id,
                campo="nome",
                valor=texto,
                commit=False,
            )
        except CanalAquisicaoError as exc:
            return _recusa_campo(exc, estado, correlation_id)
        return _apos_avanco(estado, correlation_id, acao=ACAO_REGISTRAR_NOME)
    if etapa == OnboardingCanal.ETAPA_EMAIL:
        try:
            estado = registrar_resposta_onboarding(
                identidade_id,
                campo="email",
                valor=texto,
                commit=False,
            )
        except CanalAquisicaoError as exc:
            return _recusa_campo(exc, estado, correlation_id)
        return _apos_avanco(estado, correlation_id, acao=ACAO_REGISTRAR_EMAIL)
    if etapa == OnboardingCanal.ETAPA_CARGO:
        chave = _resolver_opcao(texto, JOB_ROLES)
        if chave is None:
            return _entrada_invalida(
                estado,
                codigo=CODIGO_CARGO_INVALIDO,
                acao=ACAO_REJEITAR_CARGO,
                texto=_texto_cargos(TEXTO_CARGO_INVALIDO),
                correlation_id=correlation_id,
            )
        estado = registrar_resposta_onboarding(
            identidade_id,
            campo="job_role",
            valor=chave,
            commit=False,
        )
        return _apos_avanco(estado, correlation_id, acao=ACAO_REGISTRAR_CARGO)
    if etapa == OnboardingCanal.ETAPA_ENTREVISTA:
        pergunta = proxima_pergunta(estado.job_role, estado.respostas_entrevista)
        if pergunta is None:
            return _apresentar_termos(estado, correlation_id)
        pares = tuple((opcao.key, opcao.label) for opcao in pergunta.opcoes)
        chave = _resolver_opcao(texto, pares)
        if chave is None:
            return _entrada_invalida(
                estado,
                codigo=CODIGO_RESPOSTA_INVALIDA,
                acao=ACAO_REJEITAR_ENTREVISTA,
                texto=_texto_pergunta(pergunta, TEXTO_ENTREVISTA_INVALIDA),
                correlation_id=correlation_id,
            )
        estado = registrar_resposta_onboarding(
            identidade_id,
            campo="entrevista",
            question_key=pergunta.key,
            valor=chave,
            commit=False,
        )
        return _apos_avanco(estado, correlation_id, acao=ACAO_REGISTRAR_ENTREVISTA)
    if etapa == OnboardingCanal.ETAPA_TERMOS:
        if not _termo_efetivamente_apresentado(estado):
            return _apresentar_termos(estado, correlation_id)
        if not _aceite_explicito(texto):
            return _entrada_invalida(
                estado,
                codigo=CODIGO_ACEITE_NAO_RECONHECIDO,
                acao=ACAO_REJEITAR_ACEITE,
                texto=_texto_recusa_aceite(),
                correlation_id=correlation_id,
            )
        estado = registrar_resposta_onboarding(
            identidade_id,
            campo="declarar_aceite_termos",
            termos_referencia=estado.termos_referencia,
            aceite_declarado=True,
            commit=False,
        )
        if estado.etapa_atual == OnboardingCanal.ETAPA_SENHA:
            senha = _emitir_senha(estado, correlation_id)
            return _resultado(
                senha.codigo,
                acao=ACAO_ACEITAR_TERMOS if senha.codigo == CODIGO_LINK_EMITIDO else senha.acao,
                texto_resposta=senha.texto_resposta,
                identidade_id=senha.identidade_id,
                onboarding_id=senha.onboarding_id,
                etapa_atual=senha.etapa_atual,
                correlation_id=correlation_id,
                conclusao_id=senha.conclusao_id,
            )
        return _apos_avanco(estado, correlation_id, acao=ACAO_ACEITAR_TERMOS)
    if etapa == OnboardingCanal.ETAPA_SENHA:
        return _emitir_senha(estado, correlation_id)
    return _de_estado(
        estado,
        codigo=CODIGO_ERRO_SEGURO,
        acao=ACAO_ERRO,
        texto=TEXTO_ENCERRADA,
        correlation_id=correlation_id,
    )


def _texto_do_evento(evento: EventoCanalRecebido) -> str | None:
    if evento.tipo_evento != EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL:
        return None
    row = ConteudoTextualCanal.query.filter_by(evento_id=int(evento.id)).one_or_none()
    if row is None:
        return None
    try:
        return validar_texto_operacional(row.texto)
    except RecusaConteudo:
        return None


def _identidade_do_evento(evento: EventoCanalRecebido) -> int | None:
    if not isinstance(evento.sujeito_externo, str) or not evento.sujeito_externo:
        return None
    row = (
        IdentidadeCanalExterna.query.filter_by(
            provedor="whatsapp_meta",
            sujeito_externo=evento.sujeito_externo,
        )
        .filter(IdentidadeCanalExterna.estado != IdentidadeCanalExterna.ESTADO_REVOGADA)
        .one_or_none()
    )
    if row is None:
        return None
    return int(row.id)


def _buscar(evento_id: int) -> InterpretacaoConversacionalCanal | None:
    return InterpretacaoConversacionalCanal.query.filter_by(evento_id=evento_id).one_or_none()


def _incompleta(row: InterpretacaoConversacionalCanal) -> bool:
    return row.codigo == CODIGO_EM_TRATAMENTO or row.acao == ACAO_RESERVADA


def _assumir_incompleta(evento_id: int) -> bool:
    """Trava a mesma reserva. Não muda o código: queda deixa a linha retomável."""
    row = (
        InterpretacaoConversacionalCanal.query.filter_by(
            evento_id=evento_id,
            codigo=CODIGO_EM_TRATAMENTO,
            acao=ACAO_RESERVADA,
        )
        .with_for_update()
        .one_or_none()
    )
    return row is not None


def _reservar(evento_id: int) -> bool:
    row = InterpretacaoConversacionalCanal(
        evento_id=evento_id,
        codigo=CODIGO_EM_TRATAMENTO,
        acao=ACAO_RESERVADA,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        return False
    db.session.commit()
    return True


def _gravar(evento_id: int, resultado: ResultadoInterpretacaoCanal) -> None:
    row = _buscar(evento_id)
    if row is None:
        return
    row.codigo = resultado.codigo
    row.acao = resultado.acao or ACAO_ERRO
    row.onboarding_id = resultado.onboarding_id
    row.etapa_final = resultado.etapa_atual
    row.texto_resposta = _texto_persistivel(resultado.texto_resposta, resultado.acao)
    row.conclusao_id = resultado.conclusao_id


def _replay(
    row: InterpretacaoConversacionalCanal,
    evento: EventoCanalRecebido,
) -> ResultadoInterpretacaoCanal:
    if row.codigo == CODIGO_EM_TRATAMENTO:
        return _resultado(
            CODIGO_EVENTO_EM_TRATAMENTO,
            identidade_id=_identidade_do_evento(evento),
            correlation_id=evento.correlation_id,
        )
    onboarding_id = int(row.onboarding_id) if row.onboarding_id else None
    identidade_id = None
    if onboarding_id is not None:
        jornada = db.session.get(OnboardingCanal, onboarding_id)
        if jornada is not None:
            identidade_id = int(jornada.identidade_id)
    if identidade_id is None:
        identidade_id = _identidade_do_evento(evento)
    conclusao_id = int(row.conclusao_id) if row.conclusao_id is not None else None
    return _resultado(
        CODIGO_EVENTO_JA_TRATADO,
        acao=row.acao,
        texto_resposta=row.texto_resposta,
        identidade_id=identidade_id,
        onboarding_id=onboarding_id,
        etapa_atual=row.etapa_final,
        correlation_id=evento.correlation_id,
        conclusao_id=conclusao_id,
    )


def _produzir(
    evento: EventoCanalRecebido,
    *,
    rota: str,
    identidade_id: int,
    texto: str,
) -> ResultadoInterpretacaoCanal:
    correlation_id = evento.correlation_id
    if rota == ROTA_GUEST:
        return _guest(identidade_id, texto, correlation_id, evento)
    if rota == ROTA_ONBOARDING:
        return _onboarding(identidade_id, texto, correlation_id)
    return _resultado(
        CODIGO_ROTA_NAO_OPERADA,
        acao=ACAO_IGNORAR_ROTA,
        identidade_id=identidade_id,
        correlation_id=correlation_id,
    )


def _encerrar(
    evento_id: int,
    resultado: ResultadoInterpretacaoCanal,
) -> ResultadoInterpretacaoCanal:
    texto = _texto_persistivel(resultado.texto_resposta, resultado.acao)
    if texto != resultado.texto_resposta:
        resultado = _resultado(
            resultado.codigo,
            acao=resultado.acao,
            texto_resposta=texto,
            identidade_id=resultado.identidade_id,
            onboarding_id=resultado.onboarding_id,
            etapa_atual=resultado.etapa_atual,
            correlation_id=resultado.correlation_id,
            conclusao_id=resultado.conclusao_id,
        )
    _gravar(evento_id, resultado)
    db.session.commit()
    return resultado


def _interpretar_evento_travado(roteamento: object, evento_id: int) -> ResultadoInterpretacaoCanal:
    evento = db.session.get(EventoCanalRecebido, evento_id)
    if evento is None:
        resultado = _resultado(CODIGO_EVENTO_AUSENTE, acao=ACAO_ERRO)
        _log(resultado, evento_id)
        return resultado
    gravado = _buscar(evento_id)
    if gravado is not None and not _incompleta(gravado):
        resultado = _replay(gravado, evento)
        _log(resultado, evento_id)
        return resultado
    rota = getattr(roteamento, "rota", None)
    correlation_id = evento.correlation_id
    if rota not in _ROTAS_OPERADAS:
        resultado = _resultado(
            CODIGO_ROTA_NAO_OPERADA if isinstance(rota, str) else CODIGO_ROTEAMENTO_INDISPONIVEL,
            acao=ACAO_IGNORAR_ROTA,
            identidade_id=getattr(roteamento, "identidade_id", None)
            if _id_valido(getattr(roteamento, "identidade_id", None))
            else None,
            correlation_id=correlation_id,
        )
        _log(resultado, evento_id)
        return resultado
    if evento.status_processamento != EventoCanalRecebido.STATUS_ROTEADO:
        resultado = _resultado(
            CODIGO_EVENTO_NAO_ROTEADO,
            acao=ACAO_ERRO,
            correlation_id=correlation_id,
        )
        _log(resultado, evento_id)
        return resultado
    identidade_id = getattr(roteamento, "identidade_id", None)
    if not _id_valido(identidade_id):
        identidade_id = _identidade_do_evento(evento)
    if not _id_valido(identidade_id):
        resultado = _resultado(
            CODIGO_JORNADA_INDISPONIVEL,
            acao=ACAO_ERRO,
            correlation_id=correlation_id,
        )
        _log(resultado, evento_id)
        return resultado
    identidade_id = int(identidade_id)
    texto = _texto_do_evento(evento)
    if texto is None:
        resultado = _resultado(
            CODIGO_TEXTO_INDISPONIVEL,
            acao=ACAO_ERRO,
            identidade_id=identidade_id,
            correlation_id=correlation_id,
        )
        _log(resultado, evento_id)
        return resultado
    if gravado is None:
        if not _reservar(evento_id):
            repetido = _buscar(evento_id)
            if repetido is not None and not _incompleta(repetido):
                resultado = _replay(repetido, evento)
                _log(resultado, evento_id)
                return resultado
            if repetido is None or not _assumir_incompleta(evento_id):
                resultado = _resultado(
                    CODIGO_EVENTO_EM_TRATAMENTO,
                    acao=ACAO_ERRO,
                    identidade_id=identidade_id,
                    correlation_id=correlation_id,
                )
                _log(resultado, evento_id)
                return resultado
    elif not _assumir_incompleta(evento_id):
        atual = _buscar(evento_id)
        if atual is not None and not _incompleta(atual):
            resultado = _replay(atual, evento)
            _log(resultado, evento_id)
            return resultado
        resultado = _resultado(
            CODIGO_EVENTO_EM_TRATAMENTO,
            acao=ACAO_ERRO,
            identidade_id=identidade_id,
            correlation_id=correlation_id,
        )
        _log(resultado, evento_id)
        return resultado
    try:
        produzido = _produzir(
            evento,
            rota=str(rota),
            identidade_id=int(identidade_id),
            texto=texto,
        )
    except Exception:
        db.session.rollback()
        produzido = _resultado(
            CODIGO_ERRO_SEGURO,
            acao=ACAO_ERRO,
            identidade_id=int(identidade_id),
            correlation_id=correlation_id,
        )
    try:
        produzido = _encerrar(evento_id, produzido)
    except Exception:
        db.session.rollback()
        produzido = _resultado(
            CODIGO_ERRO_SEGURO,
            acao=ACAO_ERRO,
            identidade_id=int(identidade_id),
            correlation_id=correlation_id,
        )
    _log(produzido, evento_id)
    return produzido


def interpretar_mensagem_canal(roteamento: object) -> ResultadoInterpretacaoCanal:
    """Trata guest ou onboarding uma vez por evento. Não reenvia a mutação."""
    evento_id = getattr(roteamento, "evento_id", None)
    if not _id_valido(evento_id):
        resultado = _resultado(CODIGO_EVENTO_INVALIDO, acao=ACAO_ERRO)
        _log(resultado, evento_id)
        return resultado
    evento_id = int(evento_id)
    identidade_roteada = getattr(roteamento, "identidade_id", None)
    trava_pessoa = (
        _trava_identidade(int(identidade_roteada))
        if _id_valido(identidade_roteada)
        else nullcontext()
    )
    with _trava(evento_id):
        with trava_pessoa:
            return _interpretar_evento_travado(roteamento, evento_id)
