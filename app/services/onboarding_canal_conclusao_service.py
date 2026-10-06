"""
Conclusão da jornada de canal: link de uso único, conta nova ou vínculo.

O token é assinado e o banco guarda só o hash. A conclusão de conta nova
cria User, Conta e Franquia na mesma transação que materializa termos,
entrevista, identidade e o consumo do token. Não envia mensagem, não chama
Meta e não emite Growth.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

from app.auth_services import CadastroLocalError, montar_usuario_cadastro_local
from app.extensions import db
from app.models import (
    IdentidadeCanalExterna,
    OnboardingCanal,
    OnboardingCanalConclusao,
    User,
    utcnow_naive,
)
from app.services import canal_aquisicao_service as canal
from app.services.onboarding_entrevista_definicao import (
    ORIGEM_ONBOARDING_WHATSAPP,
    entrevista_encerrada,
)
from app.services.senha_cadastro import validar_senha_cadastro

logger = logging.getLogger(__name__)

SALT_CONCLUSAO_ONBOARDING = "onboarding-canal-conclusao-v1"
VALIDADE_LINK_CONCLUSAO = timedelta(hours=24)
_TOKEN_MAX_CHARS = 500
# 16 bytes = 128 bits. token_urlsafe produz 22 caracteres base64url, sem padding.
ALIAS_ENTROPIA_BYTES = 16
ALIAS_TAMANHO = 22
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

CODIGO_LINK_EMITIDO = "link_emitido"
CODIGO_CONTA_EXISTENTE = "existing_account_verification_required"
CODIGO_TOKEN_INVALIDO = "token_invalido"
CODIGO_TOKEN_EXPIRADO = "token_expirado"
CODIGO_TOKEN_REVOGADO = "token_revogado"
CODIGO_LINK_JA_UTILIZADO = "link_ja_utilizado"
CODIGO_JA_REALIZADA = "conclusao_ja_realizada"
CODIGO_JORNADA_NAO_OPERAVEL = "jornada_nao_operavel"
CODIGO_JORNADA_EXPIRADA = "jornada_expirada"
CODIGO_JORNADA_CANCELADA = "jornada_cancelada"
CODIGO_JORNADA_AUSENTE = "jornada_ausente"
CODIGO_IDENTIDADE_BLOQUEADA = "identidade_bloqueada"
CODIGO_IDENTIDADE_REVOGADA = "identidade_revogada"
CODIGO_IDENTIDADE_INDISPONIVEL = "identidade_indisponivel"
CODIGO_SENHA_INVALIDA = "senha_invalida"
CODIGO_SENHA_DIVERGENTE = "senha_divergente"
CODIGO_CONTA_CRIADA = "conta_criada"
CODIGO_VINCULO_CONFIRMADO = "vinculo_confirmado"
CODIGO_VINCULO_DIVERGENTE = "vinculo_conta_divergente"
CODIGO_AUTH_NECESSARIA = "autenticacao_necessaria"
CODIGO_VINCULO_NAO_APLICAVEL = "vinculo_nao_aplicavel"
CODIGO_CONFLITO = "conclusao_conflito"
CODIGO_FALHA = "falha_interna"
CODIGO_REEMISSAO_RECUSADA = "reemissao_recusada"

_travas: dict[int, threading.Lock] = {}
_travas_guard = threading.Lock()


class _Recusa(Exception):
    def __init__(self, resultado: "ResultadoConclusaoOnboarding"):
        self.resultado = resultado
        super().__init__(resultado.codigo)


class _Conflito(Exception):
    def __init__(self, onboarding_id: int):
        self.onboarding_id = onboarding_id
        super().__init__("conclusao_conflito")


@dataclass
class _Assinatura:
    conclusao_id: int
    onboarding_id: int


@dataclass
class ResultadoConclusaoOnboarding:
    codigo: str
    onboarding_id: int | None = None
    identidade_id: int | None = None
    user_id: int | None = None
    token: str | None = None
    url: str | None = None
    expira_em: datetime | None = None
    persistiu: bool = False
    autenticar: bool = False
    formulario: str | None = None
    nome_mascarado: str | None = None
    email_mascarado: str | None = None
    conclusao_id: int | None = None
    finalidade: str | None = None


def mascarar_nome(nome: str | None) -> str:
    texto = (nome or "").strip()
    if not texto:
        return ""
    return texto[0] + "***"


def mascarar_email(email: str | None) -> str:
    local, sep, domain = (email or "").partition("@")
    if not sep or not local or not domain:
        return ""
    return f"{local[0]}***@{domain}"


def _log(resultado: ResultadoConclusaoOnboarding) -> None:
    logger.info(
        "onboarding_canal_conclusao codigo=%s onboarding_id=%s identidade_id=%s",
        resultado.codigo,
        resultado.onboarding_id,
        resultado.identidade_id,
    )


def _resultado(
    codigo: str,
    *,
    onboarding_id: int | None = None,
    identidade_id: int | None = None,
    user_id: int | None = None,
    token: str | None = None,
    url: str | None = None,
    expira_em: datetime | None = None,
    persistiu: bool = False,
    autenticar: bool = False,
    formulario: str | None = None,
    nome_mascarado: str | None = None,
    email_mascarado: str | None = None,
    conclusao_id: int | None = None,
    finalidade: str | None = None,
) -> ResultadoConclusaoOnboarding:
    return ResultadoConclusaoOnboarding(
        codigo=codigo,
        onboarding_id=onboarding_id,
        identidade_id=identidade_id,
        user_id=user_id,
        token=token,
        url=url,
        expira_em=expira_em,
        persistiu=persistiu,
        autenticar=autenticar,
        formulario=formulario,
        nome_mascarado=nome_mascarado,
        email_mascarado=email_mascarado,
        conclusao_id=conclusao_id,
        finalidade=finalidade,
    )


def _trava(onboarding_id: int) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(onboarding_id)
        if lock is None:
            lock = threading.Lock()
            _travas[onboarding_id] = lock
        return lock


def _secret(secret_key: str) -> str | None:
    if not isinstance(secret_key, str) or not secret_key.strip():
        return None
    return secret_key


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=SALT_CONCLUSAO_ONBOARDING)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_aleatorio() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _hash_alias(alias: str) -> str:
    return hashlib.sha256(alias.encode("utf-8")).hexdigest()


def _alias_formato_valido(alias: object) -> bool:
    return isinstance(alias, str) and _ALIAS_RE.fullmatch(alias) is not None


def gerar_alias_opaco() -> str:
    """Alias aleatório. Não carrega id, e-mail nem telefone e não é reversível."""
    alias = secrets.token_urlsafe(ALIAS_ENTROPIA_BYTES)
    if not _alias_formato_valido(alias):
        return ""
    return alias


def alias_confere(alias: str, row: OnboardingCanalConclusao) -> bool:
    if not _alias_formato_valido(alias) or not isinstance(row.alias_hash, str):
        return False
    digest = _hash_alias(alias)
    if len(digest) != len(row.alias_hash):
        return False
    return hmac.compare_digest(row.alias_hash, digest)


def associar_alias_conclusao(conclusao_id: int) -> str | None:
    """Grava só o hash. O valor bruto volta para a URL e não é persistido."""
    if isinstance(conclusao_id, bool) or not isinstance(conclusao_id, int) or conclusao_id <= 0:
        return None
    row = db.session.get(OnboardingCanalConclusao, conclusao_id)
    if row is None or row.estado != OnboardingCanalConclusao.ESTADO_EMITIDO:
        return None
    if row.alias_hash is not None:
        return None
    alias = gerar_alias_opaco()
    if not alias:
        return None
    digest = _hash_alias(alias)
    ocupado = OnboardingCanalConclusao.query.filter_by(alias_hash=digest).first()
    if ocupado is not None:
        return None
    row.alias_hash = digest
    db.session.flush()
    return alias


def _email(valor: str | None) -> str:
    return (valor or "").strip().lower()


def _ler_assinatura(token: str, secret_key: str) -> _Assinatura | ResultadoConclusaoOnboarding:
    secret = _secret(secret_key)
    if secret is None or not isinstance(token, str) or not token or len(token) > _TOKEN_MAX_CHARS:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    max_age = int(VALIDADE_LINK_CONCLUSAO.total_seconds())
    if max_age <= 0:
        return _resultado(CODIGO_TOKEN_EXPIRADO)
    try:
        data = _serializer(secret).loads(token, max_age=max_age)
    except SignatureExpired:
        return _resultado(CODIGO_TOKEN_EXPIRADO)
    except BadSignature:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    if not isinstance(data, dict) or set(data) != {"c", "o"}:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    conclusao_id = data.get("c")
    onboarding_id = data.get("o")
    if isinstance(conclusao_id, bool) or isinstance(onboarding_id, bool):
        return _resultado(CODIGO_TOKEN_INVALIDO)
    if not isinstance(conclusao_id, int) or not isinstance(onboarding_id, int):
        return _resultado(CODIGO_TOKEN_INVALIDO)
    if conclusao_id <= 0 or onboarding_id <= 0:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    return _Assinatura(conclusao_id=conclusao_id, onboarding_id=onboarding_id)


def _mascaras(jornada: OnboardingCanal) -> tuple[str, str]:
    return mascarar_nome(jornada.nome), mascarar_email(jornada.email_normalizado)


def _recusar_identidade(identidade: IdentidadeCanalExterna) -> str | None:
    if identidade.estado == IdentidadeCanalExterna.ESTADO_BLOQUEADA:
        return CODIGO_IDENTIDADE_BLOQUEADA
    if identidade.estado == IdentidadeCanalExterna.ESTADO_REVOGADA:
        return CODIGO_IDENTIDADE_REVOGADA
    if identidade.estado not in IdentidadeCanalExterna.ESTADOS_OPERAVEIS_GUEST:
        return CODIGO_IDENTIDADE_INDISPONIVEL
    return None


def _codigo_jornada_fechada(jornada: OnboardingCanal) -> str | None:
    if jornada.etapa == OnboardingCanal.ETAPA_CANCELADO:
        return CODIGO_JORNADA_CANCELADA
    if jornada.etapa == OnboardingCanal.ETAPA_EXPIRADO:
        return CODIGO_JORNADA_EXPIRADA
    if jornada.etapa == OnboardingCanal.ETAPA_CONCLUIDO:
        return CODIGO_JA_REALIZADA
    return None


def _carregar_conclusao(conclusao_id: int) -> OnboardingCanalConclusao | None:
    query = db.session.query(OnboardingCanalConclusao).filter_by(id=conclusao_id)
    bind = db.session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        query = query.with_for_update()
    return query.one_or_none()


def _email_cadastrado(email: str) -> User | None:
    return (
        User.query.filter(func.lower(User.email) == email)
        .order_by(User.id.asc())
        .first()
    )


def _jornada_pronta_para_senha(jornada: OnboardingCanal) -> str | None:
    if jornada.etapa != OnboardingCanal.ETAPA_SENHA:
        return CODIGO_JORNADA_NAO_OPERAVEL
    if not (jornada.nome and jornada.email_normalizado and jornada.job_role):
        return CODIGO_JORNADA_NAO_OPERAVEL
    if (
        jornada.termos_apresentados_em is None
        or not jornada.termos_referencia
        or jornada.termos_aceitos_em is None
    ):
        return CODIGO_JORNADA_NAO_OPERAVEL
    respostas = canal._ler_respostas(jornada)
    if not entrevista_encerrada(jornada.job_role, respostas):
        return CODIGO_JORNADA_NAO_OPERAVEL
    if jornada.taxonomia_versao != canal.TAXONOMIA_VERSAO:
        return CODIGO_JORNADA_NAO_OPERAVEL
    return None


def _revogar_emitidos(onboarding_id: int, agora: datetime) -> None:
    rows = OnboardingCanalConclusao.query.filter_by(
        onboarding_id=onboarding_id,
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
    ).all()
    for row in rows:
        row.estado = OnboardingCanalConclusao.ESTADO_REVOGADO
        row.token_hash = _hash_aleatorio()
        row.atualizada_em = agora
    if rows:
        db.session.flush()


def _criar_token(
    jornada: OnboardingCanal,
    finalidade: str,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None,
    agora: datetime,
) -> ResultadoConclusaoOnboarding:
    _revogar_emitidos(int(jornada.id), agora)
    row = OnboardingCanalConclusao(
        onboarding_id=int(jornada.id),
        finalidade=finalidade,
        token_hash=_hash_aleatorio(),
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        emitido_em=agora,
        expira_em=agora + VALIDADE_LINK_CONCLUSAO,
        atualizada_em=agora,
    )
    db.session.add(row)
    db.session.flush()
    token = _serializer(secret_key).dumps({"c": int(row.id), "o": int(jornada.id)})
    row.token_hash = _hash_token(token)
    db.session.flush()
    url = build_url(token) if build_url is not None else None
    return _resultado(
        CODIGO_LINK_EMITIDO,
        onboarding_id=int(jornada.id),
        identidade_id=int(jornada.identidade_id),
        token=token,
        url=url,
        expira_em=row.expira_em,
        persistiu=True,
        conclusao_id=int(row.id),
        finalidade=finalidade,
    )


def _carregar_jornada_operavel(identidade_id: int) -> tuple[OnboardingCanal | None, IdentidadeCanalExterna | None, str | None]:
    try:
        identidade_id = canal._exigir_id(identidade_id)
    except canal.CanalAquisicaoError as exc:
        return None, None, exc.codigo
    identidade = db.session.get(IdentidadeCanalExterna, identidade_id)
    if identidade is None:
        return None, None, CODIGO_IDENTIDADE_INDISPONIVEL
    db.session.refresh(identidade)
    jornada = (
        OnboardingCanal.query.filter_by(identidade_id=identidade.id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .order_by(OnboardingCanal.id.desc())
        .first()
    )
    if jornada is None:
        recente = (
            OnboardingCanal.query.filter_by(identidade_id=identidade.id)
            .order_by(OnboardingCanal.id.desc())
            .first()
        )
        if recente is None:
            return None, identidade, CODIGO_JORNADA_AUSENTE
        fechada = _codigo_jornada_fechada(recente)
        return recente, identidade, fechada or CODIGO_JORNADA_NAO_OPERAVEL
    db.session.refresh(jornada)
    codigo_identidade = _recusar_identidade(identidade)
    if codigo_identidade:
        return jornada, identidade, codigo_identidade
    if canal._expirar_se_vencida(jornada):
        return jornada, identidade, CODIGO_JORNADA_EXPIRADA
    return jornada, identidade, None


def _id_positivo(valor: object) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        return None
    return valor


def _carregar_jornada_especifica(
    identidade_id: int,
    onboarding_id: int,
) -> tuple[OnboardingCanal | None, IdentidadeCanalExterna | None, str | None]:
    """Carrega só a jornada pedida. Não escolhe outra, nem a aberta mais recente."""
    try:
        identidade_id = canal._exigir_id(identidade_id)
    except canal.CanalAquisicaoError as exc:
        return None, None, exc.codigo
    if _id_positivo(onboarding_id) is None:
        return None, None, CODIGO_JORNADA_AUSENTE
    identidade = db.session.get(IdentidadeCanalExterna, identidade_id)
    if identidade is None:
        return None, None, CODIGO_IDENTIDADE_INDISPONIVEL
    db.session.refresh(identidade)
    jornada = db.session.get(OnboardingCanal, int(onboarding_id))
    if jornada is None or int(jornada.identidade_id) != int(identidade.id):
        return None, identidade, CODIGO_JORNADA_AUSENTE
    db.session.refresh(jornada)
    if jornada.etapa not in OnboardingCanal.ETAPAS_ABERTAS:
        fechada = _codigo_jornada_fechada(jornada)
        return jornada, identidade, fechada or CODIGO_JORNADA_NAO_OPERAVEL
    codigo_identidade = _recusar_identidade(identidade)
    if codigo_identidade:
        return jornada, identidade, codigo_identidade
    if canal._expirar_se_vencida(jornada):
        return jornada, identidade, CODIGO_JORNADA_EXPIRADA
    return jornada, identidade, None


def _emitir(
    identidade_id: int,
    finalidade: str,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None,
    commit: bool,
    onboarding_id: int | None = None,
) -> ResultadoConclusaoOnboarding:
    secret = _secret(secret_key)
    if secret is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado

    def _trabalho() -> ResultadoConclusaoOnboarding:
        if onboarding_id is None:
            jornada, identidade, codigo = _carregar_jornada_operavel(identidade_id)
        else:
            jornada, identidade, codigo = _carregar_jornada_especifica(
                identidade_id,
                onboarding_id,
            )
        if codigo == CODIGO_JORNADA_EXPIRADA and jornada is not None:
            raise _Recusa(
                _resultado(
                    CODIGO_JORNADA_EXPIRADA,
                    onboarding_id=int(jornada.id),
                    identidade_id=int(jornada.identidade_id),
                    persistiu=True,
                )
            )
        if codigo or jornada is None or identidade is None:
            raise _Recusa(
                _resultado(
                    codigo or CODIGO_JORNADA_AUSENTE,
                    onboarding_id=int(jornada.id) if jornada is not None else None,
                    identidade_id=int(identidade.id) if identidade is not None else None,
                )
            )
        email = jornada.email_normalizado or ""
        existente = _email_cadastrado(email) if email else None
        if finalidade == OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA:
            recusa = _jornada_pronta_para_senha(jornada)
            if recusa:
                raise _Recusa(
                    _resultado(
                        recusa,
                        onboarding_id=int(jornada.id),
                        identidade_id=int(identidade.id),
                    )
                )
            if existente is not None:
                raise _Recusa(
                    _resultado(
                        CODIGO_CONTA_EXISTENTE,
                        onboarding_id=int(jornada.id),
                        identidade_id=int(identidade.id),
                        user_id=int(existente.id),
                    )
                )
        else:
            if existente is None or not email:
                raise _Recusa(
                    _resultado(
                        CODIGO_VINCULO_NAO_APLICAVEL,
                        onboarding_id=int(jornada.id),
                        identidade_id=int(identidade.id),
                    )
                )
        return _criar_token(
            jornada,
            finalidade,
            secret_key=secret,
            build_url=build_url,
            agora=utcnow_naive(),
        )

    resultado = _executar(0, _trabalho, commit, travar=False)
    _log(resultado)
    return resultado


def emitir_link_conclusao_onboarding(
    identidade_id: int,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None = None,
    commit: bool = True,
    onboarding_id: int | None = None,
) -> ResultadoConclusaoOnboarding:
    """Emite o link de senha. Não cria User. E-mail já cadastrado não emite token.

    Com onboarding_id, a emissão vale só para essa jornada. Sem o argumento,
    permanece a jornada aberta da identidade.
    """
    return _emitir_com_trava(
        identidade_id,
        OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA,
        secret_key=secret_key,
        build_url=build_url,
        commit=commit,
        onboarding_id=onboarding_id,
    )


def emitir_link_vinculo_conta_existente(
    identidade_id: int,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None = None,
    commit: bool = True,
    onboarding_id: int | None = None,
) -> ResultadoConclusaoOnboarding:
    """Emite o link de vínculo quando o e-mail já pertence a um User.

    Com onboarding_id, a emissão vale só para essa jornada.
    """
    return _emitir_com_trava(
        identidade_id,
        OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA,
        secret_key=secret_key,
        build_url=build_url,
        commit=commit,
        onboarding_id=onboarding_id,
    )


def reemitir_link_conclusao_onboarding(
    identidade_id: int,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None = None,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Novo token para jornada ainda válida. O token anterior deixa de valer."""
    jornada, _identidade, codigo = _ler_jornada_para_reemissao(identidade_id)
    if codigo == CODIGO_CONTA_EXISTENTE:
        return emitir_link_vinculo_conta_existente(
            identidade_id,
            secret_key=secret_key,
            build_url=build_url,
            commit=commit,
        )
    if codigo:
        resultado = _resultado(
            CODIGO_REEMISSAO_RECUSADA if codigo == CODIGO_JORNADA_NAO_OPERAVEL else codigo,
            onboarding_id=int(jornada.id) if jornada is not None else None,
            identidade_id=int(jornada.identidade_id) if jornada is not None else None,
        )
        _log(resultado)
        return resultado
    return emitir_link_conclusao_onboarding(
        identidade_id,
        secret_key=secret_key,
        build_url=build_url,
        commit=commit,
    )


def _ler_jornada_para_reemissao(
    identidade_id: int,
) -> tuple[OnboardingCanal | None, IdentidadeCanalExterna | None, str | None]:
    try:
        with canal._unidade_transacional():
            jornada, identidade, codigo = _carregar_jornada_operavel(identidade_id)
            if codigo is None and jornada is None:
                codigo = CODIGO_JORNADA_AUSENTE
            elif codigo is None and jornada is not None:
                if _email_cadastrado(jornada.email_normalizado or ""):
                    codigo = CODIGO_CONTA_EXISTENTE
                elif jornada.etapa != OnboardingCanal.ETAPA_SENHA:
                    codigo = CODIGO_JORNADA_NAO_OPERAVEL
        if codigo == CODIGO_JORNADA_EXPIRADA:
            db.session.commit()
        return jornada, identidade, codigo
    except Exception:
        db.session.rollback()
        raise


def _emitir_com_trava(
    identidade_id: int,
    finalidade: str,
    *,
    secret_key: str,
    build_url: Callable[[str], str] | None,
    commit: bool,
    onboarding_id: int | None = None,
) -> ResultadoConclusaoOnboarding:
    if onboarding_id is not None:
        alvo = _id_positivo(onboarding_id)
        if alvo is None:
            return _emitir(
                identidade_id,
                finalidade,
                secret_key=secret_key,
                build_url=build_url,
                commit=commit,
                onboarding_id=onboarding_id,
            )
        with _trava(alvo):
            return _emitir(
                identidade_id,
                finalidade,
                secret_key=secret_key,
                build_url=build_url,
                commit=commit,
                onboarding_id=alvo,
            )
    aberto = _onboarding_aberto_id(identidade_id)
    if aberto is None:
        return _emitir(
            identidade_id,
            finalidade,
            secret_key=secret_key,
            build_url=build_url,
            commit=commit,
        )
    with _trava(aberto):
        return _emitir(
            identidade_id,
            finalidade,
            secret_key=secret_key,
            build_url=build_url,
            commit=commit,
        )


def _onboarding_aberto_id(identidade_id: int) -> int | None:
    if isinstance(identidade_id, bool) or not isinstance(identidade_id, int):
        return None
    jornada = (
        OnboardingCanal.query.filter_by(identidade_id=identidade_id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .order_by(OnboardingCanal.id.desc())
        .first()
    )
    if jornada is None:
        return None
    return int(jornada.id)


def inspecionar_link_conclusao(
    token: str,
    *,
    secret_key: str,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Valida o link para a página. Não autentica e não cria conta."""
    assinatura = _ler_assinatura(token, secret_key)
    if isinstance(assinatura, ResultadoConclusaoOnboarding):
        _log(assinatura)
        return assinatura
    with _trava(assinatura.onboarding_id):
        resultado = _executar(
            assinatura.onboarding_id,
            lambda: _inspecionar_dentro(token, assinatura),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def inspecionar_alias_conclusao(
    alias: str,
    *,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Abre a conclusão pelo alias curto. Não reconstrói o token assinado."""
    onboarding_id = _onboarding_do_alias(alias)
    if onboarding_id is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(onboarding_id):
        resultado = _executar(
            onboarding_id,
            lambda: _inspecionar_alias_dentro(alias),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def concluir_definicao_senha_por_alias(
    alias: str,
    password: str,
    confirm_password: str,
    *,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Cria a conta pelo alias curto. O consumo é o mesmo do token assinado."""
    onboarding_id = _onboarding_do_alias(alias)
    if onboarding_id is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(onboarding_id):
        resultado = _executar(
            onboarding_id,
            lambda: _concluir_senha_alias_dentro(alias, password, confirm_password),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def confirmar_vinculo_conta_por_alias(
    alias: str,
    user: User | None,
    *,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Confirma o vínculo pelo alias curto, com as mesmas regras do token."""
    onboarding_id = _onboarding_do_alias(alias)
    if onboarding_id is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(onboarding_id):
        resultado = _executar(
            onboarding_id,
            lambda: _confirmar_vinculo_alias_dentro(alias, user),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def concluir_definicao_senha(
    token: str,
    password: str,
    confirm_password: str,
    *,
    secret_key: str,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Cria a conta nova e consome o token. E-mail já existente não cria User."""
    assinatura = _ler_assinatura(token, secret_key)
    if isinstance(assinatura, ResultadoConclusaoOnboarding):
        _log(assinatura)
        return assinatura
    with _trava(assinatura.onboarding_id):
        resultado = _executar(
            assinatura.onboarding_id,
            lambda: _concluir_senha_dentro(token, assinatura, password, confirm_password),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def confirmar_vinculo_conta_existente(
    token: str,
    user: User | None,
    *,
    secret_key: str,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Vincula a identidade ao User já autenticado, se o e-mail for o da jornada."""
    assinatura = _ler_assinatura(token, secret_key)
    if isinstance(assinatura, ResultadoConclusaoOnboarding):
        _log(assinatura)
        return assinatura
    with _trava(assinatura.onboarding_id):
        resultado = _executar(
            assinatura.onboarding_id,
            lambda: _confirmar_vinculo_dentro(token, assinatura, user),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def inspecionar_handoff_conclusao(
    conclusao_id: int,
    *,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Relê a conclusão apontada pelo handoff. Não autentica e não consome o token."""
    onboarding_id = _onboarding_do_handoff(conclusao_id)
    if onboarding_id is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(onboarding_id):
        resultado = _executar(
            onboarding_id,
            lambda: _inspecionar_handoff_dentro(conclusao_id),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def confirmar_vinculo_handoff(
    conclusao_id: int,
    user: User | None,
    *,
    commit: bool = True,
) -> ResultadoConclusaoOnboarding:
    """Confirma o vínculo da conclusão já aberta na sessão. Exige o User e o e-mail."""
    onboarding_id = _onboarding_do_handoff(conclusao_id)
    if onboarding_id is None:
        resultado = _resultado(CODIGO_TOKEN_INVALIDO)
        _log(resultado)
        return resultado
    with _trava(onboarding_id):
        resultado = _executar(
            onboarding_id,
            lambda: _confirmar_handoff_dentro(conclusao_id, user),
            commit,
            travar=False,
        )
    _log(resultado)
    return resultado


def _localizar_alias(alias: str, *, travar: bool) -> OnboardingCanalConclusao | None:
    if not _alias_formato_valido(alias):
        return None
    digest = _hash_alias(alias)
    query = db.session.query(OnboardingCanalConclusao).filter_by(alias_hash=digest)
    if travar:
        bind = db.session.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            query = query.with_for_update()
    row = query.one_or_none()
    if row is None or not alias_confere(alias, row):
        return None
    return row


def _onboarding_do_alias(alias: str) -> int | None:
    row = _localizar_alias(alias, travar=False)
    if row is None:
        return None
    return int(row.onboarding_id)


def _abrir_alias(
    alias: str,
) -> tuple[OnboardingCanalConclusao, OnboardingCanal, IdentidadeCanalExterna] | ResultadoConclusaoOnboarding:
    row = _localizar_alias(alias, travar=True)
    if row is None:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    aberto = _abrir_por_id(int(row.id))
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    atual, _jornada, _identidade = aberto
    if not alias_confere(alias, atual):
        return _resultado(CODIGO_TOKEN_INVALIDO, onboarding_id=int(atual.onboarding_id))
    return aberto


def _onboarding_do_handoff(conclusao_id: int) -> int | None:
    if isinstance(conclusao_id, bool) or not isinstance(conclusao_id, int) or conclusao_id <= 0:
        return None
    row = db.session.get(OnboardingCanalConclusao, conclusao_id)
    if row is None:
        return None
    return int(row.onboarding_id)


def _executar(
    onboarding_id: int,
    trabalho,
    commit: bool,
    *,
    travar: bool,
) -> ResultadoConclusaoOnboarding:
    def _rodar() -> ResultadoConclusaoOnboarding:
        try:
            with canal._unidade_transacional():
                resultado = trabalho()
            if resultado.persistiu and commit:
                db.session.commit()
            return resultado
        except _Recusa as exc:
            # O SAVEPOINT já desfez a tentativa. Rollback da sessão apagaria
            # a jornada ainda não confirmada pelo chamador.
            if exc.resultado.persistiu and commit:
                return _persistir_recusa(exc.resultado)
            return exc.resultado
        except _Conflito as exc:
            db.session.rollback()
            return _recuperar_conflito(exc.onboarding_id)
        except IntegrityError:
            db.session.rollback()
            return _recuperar_conflito(onboarding_id)
        except OperationalError:
            db.session.rollback()
            return _recuperar_conflito(onboarding_id)
        except Exception:
            db.session.rollback()
            logger.info(
                "onboarding_canal_conclusao codigo=%s onboarding_id=%s",
                CODIGO_FALHA,
                onboarding_id or None,
            )
            return _resultado(CODIGO_FALHA, onboarding_id=onboarding_id or None)

    if travar and onboarding_id:
        with _trava(onboarding_id):
            return _rodar()
    return _rodar()


def _persistir_recusa(resultado: ResultadoConclusaoOnboarding) -> ResultadoConclusaoOnboarding:
    """A expiração da jornada já foi desfeita pelo rollback. Grava só esse fato."""
    if resultado.codigo != CODIGO_JORNADA_EXPIRADA or resultado.onboarding_id is None:
        resultado.persistiu = False
        return resultado
    jornada = db.session.get(OnboardingCanal, resultado.onboarding_id)
    if jornada is None:
        return resultado
    if canal._expirar_se_vencida(jornada):
        db.session.commit()
        resultado.persistiu = True
    return resultado


def _recuperar_conflito(onboarding_id: int) -> ResultadoConclusaoOnboarding:
    jornada = db.session.get(OnboardingCanal, onboarding_id) if onboarding_id else None
    if jornada is not None:
        db.session.refresh(jornada)
        if jornada.etapa == OnboardingCanal.ETAPA_CONCLUIDO:
            identidade = db.session.get(IdentidadeCanalExterna, jornada.identidade_id)
            user_id = int(identidade.user_id) if identidade and identidade.user_id else None
            return _resultado(
                CODIGO_JA_REALIZADA,
                onboarding_id=int(jornada.id),
                identidade_id=int(jornada.identidade_id),
                user_id=user_id,
            )
    return _resultado(CODIGO_CONFLITO, onboarding_id=onboarding_id or None)


def _abrir_row(
    token: str,
    assinatura: _Assinatura,
) -> tuple[OnboardingCanalConclusao, OnboardingCanal, IdentidadeCanalExterna] | ResultadoConclusaoOnboarding:
    row = _carregar_conclusao(assinatura.conclusao_id)
    if row is None or int(row.onboarding_id) != assinatura.onboarding_id:
        return _resultado(CODIGO_TOKEN_INVALIDO, onboarding_id=assinatura.onboarding_id)
    jornada = db.session.get(OnboardingCanal, int(row.onboarding_id))
    if jornada is None:
        return _resultado(CODIGO_JORNADA_AUSENTE, onboarding_id=assinatura.onboarding_id)
    db.session.refresh(jornada)
    identidade = db.session.get(IdentidadeCanalExterna, int(jornada.identidade_id))
    if identidade is None:
        return _resultado(
            CODIGO_IDENTIDADE_INDISPONIVEL,
            onboarding_id=int(jornada.id),
        )
    db.session.refresh(identidade)
    if row.estado == OnboardingCanalConclusao.ESTADO_REVOGADO:
        return _resultado(
            CODIGO_TOKEN_REVOGADO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    if row.estado == OnboardingCanalConclusao.ESTADO_CONSUMIDO:
        fechada = _codigo_jornada_fechada(jornada)
        return _resultado(
            fechada or CODIGO_LINK_JA_UTILIZADO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
            user_id=int(identidade.user_id) if identidade.user_id else None,
        )
    if not hmac.compare_digest(row.token_hash, _hash_token(token)):
        return _resultado(
            CODIGO_TOKEN_INVALIDO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    return _row_operavel(row, jornada, identidade)


def _abrir_por_id(
    conclusao_id: int,
) -> tuple[OnboardingCanalConclusao, OnboardingCanal, IdentidadeCanalExterna] | ResultadoConclusaoOnboarding:
    """Abre a conclusão já validada pelo handoff. Não recebe o token bruto."""
    if isinstance(conclusao_id, bool) or not isinstance(conclusao_id, int) or conclusao_id <= 0:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    row = _carregar_conclusao(conclusao_id)
    if row is None:
        return _resultado(CODIGO_TOKEN_INVALIDO)
    jornada = db.session.get(OnboardingCanal, int(row.onboarding_id))
    if jornada is None:
        return _resultado(CODIGO_JORNADA_AUSENTE, onboarding_id=int(row.onboarding_id))
    db.session.refresh(jornada)
    identidade = db.session.get(IdentidadeCanalExterna, int(jornada.identidade_id))
    if identidade is None:
        return _resultado(
            CODIGO_IDENTIDADE_INDISPONIVEL,
            onboarding_id=int(jornada.id),
        )
    db.session.refresh(identidade)
    if row.estado == OnboardingCanalConclusao.ESTADO_REVOGADO:
        return _resultado(
            CODIGO_TOKEN_REVOGADO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    if row.estado == OnboardingCanalConclusao.ESTADO_CONSUMIDO:
        fechada = _codigo_jornada_fechada(jornada)
        return _resultado(
            fechada or CODIGO_LINK_JA_UTILIZADO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
            user_id=int(identidade.user_id) if identidade.user_id else None,
        )
    return _row_operavel(row, jornada, identidade)


def _row_operavel(
    row: OnboardingCanalConclusao,
    jornada: OnboardingCanal,
    identidade: IdentidadeCanalExterna,
) -> tuple[OnboardingCanalConclusao, OnboardingCanal, IdentidadeCanalExterna] | ResultadoConclusaoOnboarding:
    if row.expira_em <= utcnow_naive():
        return _resultado(
            CODIGO_TOKEN_EXPIRADO,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    fechada = _codigo_jornada_fechada(jornada)
    if fechada:
        return _resultado(
            fechada,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    if canal._expirar_se_vencida(jornada):
        raise _Recusa(
            _resultado(
                CODIGO_JORNADA_EXPIRADA,
                onboarding_id=int(jornada.id),
                identidade_id=int(identidade.id),
                persistiu=True,
            )
        )
    codigo_identidade = _recusar_identidade(identidade)
    if codigo_identidade:
        return _resultado(
            codigo_identidade,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    return row, jornada, identidade


def _formulario(jornada: OnboardingCanal, finalidade: str) -> str:
    if _email_cadastrado(jornada.email_normalizado or "") is not None:
        return "vinculo"
    if finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA:
        return "vinculo"
    return "senha"


def _inspecionar_aberto(
    row: OnboardingCanalConclusao,
    jornada: OnboardingCanal,
    identidade: IdentidadeCanalExterna,
) -> ResultadoConclusaoOnboarding:
    nome, email = _mascaras(jornada)
    formulario = _formulario(jornada, row.finalidade)
    return _resultado(
        CODIGO_LINK_EMITIDO if formulario == "senha" else CODIGO_CONTA_EXISTENTE,
        onboarding_id=int(jornada.id),
        identidade_id=int(identidade.id),
        expira_em=row.expira_em,
        formulario=formulario,
        nome_mascarado=nome,
        email_mascarado=email,
    )


def _inspecionar_dentro(
    token: str,
    assinatura: _Assinatura,
) -> ResultadoConclusaoOnboarding:
    aberto = _abrir_row(token, assinatura)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _inspecionar_aberto(*aberto)


def _inspecionar_alias_dentro(alias: str) -> ResultadoConclusaoOnboarding:
    aberto = _abrir_alias(alias)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _inspecionar_aberto(*aberto)


def _concluir_senha_dentro(
    token: str,
    assinatura: _Assinatura,
    password: str,
    confirm_password: str,
) -> ResultadoConclusaoOnboarding:
    aberto = _abrir_row(token, assinatura)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _concluir_senha_aberta(*aberto, password, confirm_password)


def _concluir_senha_alias_dentro(
    alias: str,
    password: str,
    confirm_password: str,
) -> ResultadoConclusaoOnboarding:
    aberto = _abrir_alias(alias)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _concluir_senha_aberta(*aberto, password, confirm_password)


def _concluir_senha_aberta(
    row: OnboardingCanalConclusao,
    jornada: OnboardingCanal,
    identidade: IdentidadeCanalExterna,
    password: str,
    confirm_password: str,
) -> ResultadoConclusaoOnboarding:
    existente = _email_cadastrado(jornada.email_normalizado or "")
    if existente is not None or row.finalidade == OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA:
        nome, email = _mascaras(jornada)
        return _resultado(
            CODIGO_CONTA_EXISTENTE,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
            user_id=int(existente.id) if existente is not None else None,
            formulario="vinculo",
            nome_mascarado=nome,
            email_mascarado=email,
        )
    recusa = _jornada_pronta_para_senha(jornada)
    if recusa:
        return _resultado(
            recusa,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    if password != confirm_password:
        return _resultado(
            CODIGO_SENHA_DIVERGENTE,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    erro_senha = validar_senha_cadastro(password)
    if erro_senha:
        return _resultado(
            CODIGO_SENHA_INVALIDA,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    try:
        user = montar_usuario_cadastro_local(
            jornada.nome or "",
            jornada.email_normalizado or "",
            password,
            job_role=jornada.job_role or "",
            usage_purpose="",
            subscribes_to_newsletter=False,
            accept_terms=False,
            accepted_terms_at=jornada.termos_aceitos_em,
            respostas_entrevista=canal._ler_respostas(jornada),
            origem_entrevista=ORIGEM_ONBOARDING_WHATSAPP,
            cadastro_origem=User.CADASTRO_ORIGEM_WHATSAPP,
        )
    except CadastroLocalError as exc:
        if exc.mensagem == "Este e-mail já está cadastrado.":
            return _resultado(
                CODIGO_CONTA_EXISTENTE,
                onboarding_id=int(jornada.id),
                identidade_id=int(identidade.id),
                formulario="vinculo",
            )
        raise _Recusa(
            _resultado(
                CODIGO_FALHA,
                onboarding_id=int(jornada.id),
                identidade_id=int(identidade.id),
            )
        ) from exc
    agora = utcnow_naive()
    user.last_login_at = agora
    _vincular_identidade(identidade, user, agora)
    _concluir_jornada(jornada, agora)
    if not _consumir_token(int(row.id), agora):
        raise _Conflito(int(jornada.id))
    return _resultado(
        CODIGO_CONTA_CRIADA,
        onboarding_id=int(jornada.id),
        identidade_id=int(identidade.id),
        user_id=int(user.id),
        persistiu=True,
        autenticar=True,
    )


def _confirmar_vinculo_dentro(
    token: str,
    assinatura: _Assinatura,
    user: User | None,
) -> ResultadoConclusaoOnboarding:
    _travar_usuario_do_vinculo(user)
    aberto = _abrir_row(token, assinatura)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _aplicar_vinculo(*aberto, user)


def _confirmar_vinculo_alias_dentro(
    alias: str,
    user: User | None,
) -> ResultadoConclusaoOnboarding:
    _travar_usuario_do_vinculo(user)
    aberto = _abrir_alias(alias)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    return _aplicar_vinculo(*aberto, user)


def _inspecionar_handoff_dentro(conclusao_id: int) -> ResultadoConclusaoOnboarding:
    aberto = _abrir_por_id(conclusao_id)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    row, jornada, identidade = aberto
    nome, email = _mascaras(jornada)
    formulario = _formulario(jornada, row.finalidade)
    if formulario != "vinculo":
        return _resultado(
            CODIGO_VINCULO_NAO_APLICAVEL,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    return _resultado(
        CODIGO_CONTA_EXISTENTE,
        onboarding_id=int(jornada.id),
        identidade_id=int(identidade.id),
        expira_em=row.expira_em,
        formulario="vinculo",
        nome_mascarado=nome,
        email_mascarado=email,
    )


def _confirmar_handoff_dentro(
    conclusao_id: int,
    user: User | None,
) -> ResultadoConclusaoOnboarding:
    _travar_usuario_do_vinculo(user)
    aberto = _abrir_por_id(conclusao_id)
    if isinstance(aberto, ResultadoConclusaoOnboarding):
        return aberto
    row, jornada, identidade = aberto
    return _aplicar_vinculo(row, jornada, identidade, user)


def _aplicar_vinculo(
    row: OnboardingCanalConclusao,
    jornada: OnboardingCanal,
    identidade: IdentidadeCanalExterna,
    user: User | None,
) -> ResultadoConclusaoOnboarding:
    if user is None or getattr(user, "id", None) is None:
        nome, email = _mascaras(jornada)
        return _resultado(
            CODIGO_AUTH_NECESSARIA,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
            formulario="vinculo",
            nome_mascarado=nome,
            email_mascarado=email,
        )
    existente = _email_cadastrado(jornada.email_normalizado or "")
    if existente is None:
        return _resultado(
            CODIGO_VINCULO_NAO_APLICAVEL,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    if _email(getattr(user, "email", None)) != _email(jornada.email_normalizado) or int(user.id) != int(existente.id):
        return _resultado(
            CODIGO_VINCULO_DIVERGENTE,
            onboarding_id=int(jornada.id),
            identidade_id=int(identidade.id),
        )
    agora = utcnow_naive()
    _vincular_identidade(identidade, existente, agora)
    _concluir_jornada(jornada, agora)
    if not _consumir_token(int(row.id), agora):
        raise _Conflito(int(jornada.id))
    return _resultado(
        CODIGO_VINCULO_CONFIRMADO,
        onboarding_id=int(jornada.id),
        identidade_id=int(identidade.id),
        user_id=int(existente.id),
        persistiu=True,
    )


def _travar_usuario_do_vinculo(user: User | None) -> None:
    """Trava o User antes de ler a conclusão. A desconexão usa a mesma ordem."""
    if user is None or getattr(user, "id", None) is None:
        return
    from app.services.central_plugin_whatsapp_service import travar_usuario_whatsapp

    travar_usuario_whatsapp(int(user.id))


def _vincular_identidade(identidade: IdentidadeCanalExterna, user: User, agora: datetime) -> None:
    from app.services.central_plugin_whatsapp_service import (
        sincronizar_conexao_apos_vinculo,
        travar_usuario_whatsapp,
        usuario_ja_tem_outro_whatsapp,
    )

    if identidade.provedor == canal.PROVEDOR_WHATSAPP_META:
        if travar_usuario_whatsapp(int(user.id)) is None:
            raise _Recusa(
                _resultado(
                    CODIGO_VINCULO_NAO_APLICAVEL,
                    onboarding_id=None,
                    identidade_id=int(identidade.id),
                )
            )
        if usuario_ja_tem_outro_whatsapp(int(user.id), int(identidade.id)):
            raise _Recusa(
                _resultado(
                    CODIGO_VINCULO_NAO_APLICAVEL,
                    onboarding_id=None,
                    identidade_id=int(identidade.id),
                )
            )
    identidade.estado = IdentidadeCanalExterna.ESTADO_VINCULADA
    identidade.user_id = int(user.id)
    identidade.vinculada_em = agora
    identidade.revogada_em = None
    identidade.atualizada_em = agora
    sincronizar_conexao_apos_vinculo(identidade)


def _concluir_jornada(jornada: OnboardingCanal, agora: datetime) -> None:
    etapa = jornada.etapa
    if not canal._persistir_transicao(
        jornada,
        etapa,
        {
            "etapa": OnboardingCanal.ETAPA_CONCLUIDO,
            "concluida_em": agora,
            "atualizada_em": agora,
        },
    ):
        raise _Conflito(int(jornada.id))


def _consumir_token(row_id: int, agora: datetime) -> bool:
    linhas = (
        db.session.query(OnboardingCanalConclusao)
        .filter(
            OnboardingCanalConclusao.id == row_id,
            OnboardingCanalConclusao.estado == OnboardingCanalConclusao.ESTADO_EMITIDO,
            OnboardingCanalConclusao.consumido_em.is_(None),
        )
        .update(
            {
                "estado": OnboardingCanalConclusao.ESTADO_CONSUMIDO,
                "consumido_em": agora,
                "atualizada_em": agora,
            },
            synchronize_session="fetch",
        )
    )
    return linhas == 1


def revogar_conclusoes_relacionadas_ao_usuario(user: User) -> int:
    """Troca o hash dos links deste User. Não grava e-mail nem senha."""
    if getattr(user, "id", None) is None:
        return 0
    ids: set[int] = set()
    for ident in IdentidadeCanalExterna.query.filter_by(user_id=int(user.id)).all():
        for jornada in OnboardingCanal.query.filter_by(identidade_id=ident.id).all():
            ids.add(int(jornada.id))
    email = _email(getattr(user, "email", None))
    if email:
        for jornada in OnboardingCanal.query.filter(
            func.lower(OnboardingCanal.email_normalizado) == email
        ).all():
            ids.add(int(jornada.id))
    if not ids:
        return 0
    agora = utcnow_naive()
    rows = OnboardingCanalConclusao.query.filter(
        OnboardingCanalConclusao.onboarding_id.in_(ids)
    ).all()
    for row in rows:
        row.token_hash = _hash_aleatorio()
        row.atualizada_em = agora
        if row.estado == OnboardingCanalConclusao.ESTADO_EMITIDO:
            row.estado = OnboardingCanalConclusao.ESTADO_REVOGADO
    return len(rows)
