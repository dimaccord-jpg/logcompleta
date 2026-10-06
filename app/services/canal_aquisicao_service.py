"""
Domínio de aquisição por canal, antes de existir User.

A identidade externa é a pessoa no provider. A quota guest e a jornada de
onboarding ficam em tabelas próprias. Não há sessão Flask, conexão de plugin,
Growth, webhook, token, telefone nem criação de conta.

Unidade transacional: cada escrita que pode falhar por integridade entra num
SAVEPOINT já aberto. No sqlite3 em controle legado, a unidade abre BEGIN antes
desse SAVEPOINT. IntegrityError reverte só o SAVEPOINT. A transação externa
permanece utilizável. commit padrão é False: o chamador confirma o próprio
trabalho.

A quinta interação útil incrementa. A seguinte devolve guest_limit_reached
e não insere evento. O mesmo evento_externo_id não incrementa de novo.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    IdentidadeCanalExterna,
    InteracaoGuestCanal,
    OnboardingCanal,
    User,
    utcnow_naive,
)
from app.services.canal_aquisicao_config import (
    limite_interacoes_guest,
    validade_onboarding_canal,
)
from app.services.onboarding_entrevista_definicao import (
    JOB_ROLES,
    TAXONOMIA_VERSAO,
    entrevista_ativa,
    perguntas_visiveis,
    proxima_pergunta,
    validar_combinacao,
)

_PROVEDOR_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SUJEITO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,119}$")
_TELEFONE_RE = re.compile(r"^\+?\d{8,15}$")
# Identificador técnico já normalizado pelo adapter Meta: from e phone_number_id.
# Só dígitos, no tamanho que o evento consegue guardar. Sem "+", máscara ou parsing.
PROVEDOR_WHATSAPP_META = "whatsapp_meta"
_ID_TECNICO_WHATSAPP_META_RE = re.compile(r"^[0-9]{1,32}$")
_EVENTO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,119}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REFERENCIA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_ORIGEM_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

_MARCAS_SECRETAS = (
    "bearer",
    "api_key",
    "api-key",
    "access_token",
    "refresh_token",
    "token=",
    "senha=",
    "password=",
    "secret=",
    "sk-",
    "sk_live",
    "eyj",
    "webhook",
)

_CAMPOS_RESPOSTA = frozenset(
    {
        "aceitar_convite",
        "nome",
        "email",
        "job_role",
        "entrevista",
        "apresentar_termos",
        "declarar_aceite_termos",
    }
)
_CAMPOS_PROIBIDOS = frozenset(
    {"senha", "password", "token", "payload", "telefone", "phone", "webhook"}
)
_CARGOS = frozenset(chave for chave, _rotulo in JOB_ROLES)

UQ_IDENTIDADE_ATIVA = "uq_identidade_canal_provider_subject_ativa"
UQ_EVENTO_GUEST = "uq_interacao_guest_identidade_evento"
UQ_JORNADA_ABERTA = "uq_onboarding_canal_jornada_aberta"

_SQLITE_UNIQUE = (
    (
        "UNIQUE constraint failed: identidade_canal_externa.provedor, identidade_canal_externa.sujeito_externo",
        UQ_IDENTIDADE_ATIVA,
    ),
    (f"UNIQUE constraint failed: {UQ_IDENTIDADE_ATIVA}", UQ_IDENTIDADE_ATIVA),
    (
        "UNIQUE constraint failed: interacao_guest_canal.identidade_id, interacao_guest_canal.evento_externo_id",
        UQ_EVENTO_GUEST,
    ),
    (f"UNIQUE constraint failed: {UQ_EVENTO_GUEST}", UQ_EVENTO_GUEST),
    (
        "UNIQUE constraint failed: onboarding_canal.identidade_id",
        UQ_JORNADA_ABERTA,
    ),
    (f"UNIQUE constraint failed: {UQ_JORNADA_ABERTA}", UQ_JORNADA_ABERTA),
    (UQ_JORNADA_ABERTA, UQ_JORNADA_ABERTA),
)

CODIGO_LIMITE = "guest_limit_reached"
CODIGO_CONTA_EXISTENTE = "existing_account_verification_required"
CODIGO_INTERACAO_REGISTRADA = "interacao_registrada"
CODIGO_INTERACAO_REPLAY = "interacao_replay"
CODIGO_EVENTO_REPLAY = "evento_replay"
CODIGO_EVENTO_SEM_CONSUMO = "evento_nao_consumido"
CODIGO_DISPONIVEL = "disponivel"
CODIGO_TRANSICAO_CONFLITO = "transicao_conflito"

_SINAIS_NOME = frozenset("+-()./")
_MARCAS_NOME_TECNICO = (
    "authorization:",
    "proxy-authorization:",
    "bearer ",
    "api_key=",
    "api-key=",
    "token=",
    "password=",
    "secret=",
)


class CanalAquisicaoError(ValueError):
    """Erro de domínio da aquisição por canal. O código é o contrato."""

    def __init__(self, codigo: str, mensagem: str):
        self.codigo = codigo
        super().__init__(mensagem)


class MaterialSensivelRecusadoError(CanalAquisicaoError):
    def __init__(self, mensagem: str):
        super().__init__("material_sensivel", mensagem)


class IdentidadeAtivaDuplicadaError(CanalAquisicaoError):
    def __init__(self):
        super().__init__(
            "identidade_ativa_duplicada",
            "Já existe identidade ativa para este sujeito externo.",
        )


class EventoGuestDuplicadoError(CanalAquisicaoError):
    def __init__(self):
        super().__init__(
            "evento_guest_duplicado",
            "Este evento externo já foi registrado para a identidade.",
        )


class JornadaAbertaDuplicadaError(CanalAquisicaoError):
    def __init__(self):
        super().__init__(
            "jornada_aberta_duplicada",
            "Já existe jornada aberta para esta identidade.",
        )


@dataclass(frozen=True)
class EstadoGuest:
    identidade_id: int
    usadas: int
    restantes: int
    limite: int
    bloqueado_para_nova_interacao: bool
    codigo: str


@dataclass(frozen=True)
class ResultadoInteracaoGuest:
    codigo: str
    usadas: int
    restantes: int
    limite: int
    bloqueado_para_nova_interacao: bool
    consumiu: bool


@dataclass
class EstadoOnboardingCanal:
    onboarding_id: int
    identidade_id: int
    etapa_atual: str
    codigo: str
    pergunta_key: str | None
    nome: str | None
    email_normalizado: str | None
    job_role: str | None
    respostas_entrevista: dict[str, str]
    termos_apresentados: bool
    termos_referencia: str | None
    termos_aceitos: bool
    pausas_interacao_guest: int
    expira_em: datetime


def identificar_constraint_integrity_error(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    nome_pg = getattr(diag, "constraint_name", None) if diag is not None else None
    if nome_pg:
        nome = str(nome_pg)
        if nome in {UQ_IDENTIDADE_ATIVA, UQ_EVENTO_GUEST, UQ_JORNADA_ABERTA}:
            return nome
        return None
    msg = str(orig if orig is not None else exc)
    for fragmento, nome in _SQLITE_UNIQUE:
        if fragmento in msg:
            return nome
    return None


def converter_ou_relancar_integrity_error(exc: IntegrityError) -> None:
    nome = identificar_constraint_integrity_error(exc)
    if nome == UQ_IDENTIDADE_ATIVA:
        raise IdentidadeAtivaDuplicadaError from exc
    if nome == UQ_EVENTO_GUEST:
        raise EventoGuestDuplicadoError from exc
    if nome == UQ_JORNADA_ABERTA:
        raise JornadaAbertaDuplicadaError from exc
    raise exc


def _garantir_transacao_externa_sqlite() -> None:
    """Abre BEGIN antes do SAVEPOINT no sqlite3 em controle legado.

    Nesse modo, SAVEPOINT não inicia transação. RELEASE SAVEPOINT confirma a
    escrita na hora, e o rollback da sessão não a desfaz. Se o sqlite já está
    numa transação, o SAVEPOINT entra nela.
    """
    bind = db.session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "sqlite":
        return
    sa_conn = db.session.connection()
    fairy = getattr(sa_conn, "connection", None)
    bruto = getattr(fairy, "driver_connection", None)
    if bruto is None or not hasattr(bruto, "in_transaction"):
        return
    if bruto.in_transaction:
        return
    db.session.execute(text("BEGIN"))


@contextmanager
def _unidade_transacional():
    """Persiste a unidade lógica dentro de um SAVEPOINT já aberto."""
    _garantir_transacao_externa_sqlite()
    try:
        with db.session.begin_nested():
            yield
            db.session.flush()
    except IntegrityError as exc:
        converter_ou_relancar_integrity_error(exc)


def _executar(trabalho) -> None:
    with _unidade_transacional():
        trabalho()


def _finalizar(commit: bool) -> None:
    if commit:
        db.session.commit()


def _recusar_material_sensivel(valor: str, campo: str) -> None:
    baixo = valor.casefold()
    if any(marca in baixo for marca in _MARCAS_SECRETAS) or "{" in valor or "}" in valor:
        raise MaterialSensivelRecusadoError(
            f"{campo} não pode carregar segredo ou payload bruto."
        )


def _exigir_id(identidade_id: int) -> int:
    if isinstance(identidade_id, bool) or not isinstance(identidade_id, int) or identidade_id <= 0:
        raise CanalAquisicaoError("identidade_invalida", "A identidade externa é inválida.")
    return identidade_id


def _normalizar_provedor(provedor: str) -> str:
    if not isinstance(provedor, str):
        raise CanalAquisicaoError("provedor_invalido", "O provedor do canal é inválido.")
    _recusar_material_sensivel(provedor, "provedor")
    texto_provedor = provedor.strip().lower()
    if not _PROVEDOR_RE.fullmatch(texto_provedor):
        raise CanalAquisicaoError("provedor_invalido", "O provedor do canal é inválido.")
    return texto_provedor


def _identificador_tecnico_whatsapp_meta(provedor: str, valor: str) -> bool:
    """Dígitos opacos do contrato Meta. Não é telefone e não vale para outro provedor."""
    return (
        provedor == PROVEDOR_WHATSAPP_META
        and _ID_TECNICO_WHATSAPP_META_RE.fullmatch(valor) is not None
    )


def _formato_opaco_recusado(valor: str, provedor: str) -> bool:
    if _identificador_tecnico_whatsapp_meta(provedor, valor):
        return False
    return _TELEFONE_RE.fullmatch(valor) is not None or _SUJEITO_RE.fullmatch(valor) is None


def _normalizar_sujeito(sujeito_externo: str, provedor: str) -> str:
    if not isinstance(sujeito_externo, str):
        raise CanalAquisicaoError(
            "sujeito_externo_invalido",
            "O sujeito externo do canal é inválido.",
        )
    _recusar_material_sensivel(sujeito_externo, "sujeito_externo")
    texto_sujeito = sujeito_externo.strip()
    if _formato_opaco_recusado(texto_sujeito, provedor):
        raise CanalAquisicaoError(
            "sujeito_externo_invalido",
            "O sujeito externo precisa ser um identificador opaco, sem telefone.",
        )
    return texto_sujeito


def _normalizar_destino(contexto_destino: str | None, provedor: str) -> str | None:
    if contexto_destino is None:
        return None
    if not isinstance(contexto_destino, str):
        raise CanalAquisicaoError(
            "contexto_destino_invalido",
            "O contexto de destino do canal é inválido.",
        )
    _recusar_material_sensivel(contexto_destino, "contexto_destino")
    texto_destino = contexto_destino.strip()
    if not texto_destino:
        return None
    if _formato_opaco_recusado(texto_destino, provedor):
        raise CanalAquisicaoError(
            "contexto_destino_invalido",
            "O contexto de destino precisa ser um identificador opaco.",
        )
    return texto_destino


def _normalizar_evento(evento_externo_id: str) -> str:
    if not isinstance(evento_externo_id, str):
        raise CanalAquisicaoError("evento_externo_invalido", "O evento externo é inválido.")
    _recusar_material_sensivel(evento_externo_id, "evento_externo_id")
    texto_evento = evento_externo_id.strip()
    if not _EVENTO_RE.fullmatch(texto_evento):
        raise CanalAquisicaoError("evento_externo_invalido", "O evento externo é inválido.")
    return texto_evento


def _normalizar_aquisicao(
    origem_aquisicao: str | None,
    correlation_id: str | None,
) -> tuple[str | None, str | None]:
    origem = None
    correlation = None
    if origem_aquisicao is not None:
        if not isinstance(origem_aquisicao, str):
            raise CanalAquisicaoError(
                "origem_aquisicao_invalida",
                "A origem de aquisição é inválida.",
            )
        _recusar_material_sensivel(origem_aquisicao, "origem_aquisicao")
        origem = origem_aquisicao.strip().lower()
        if not _ORIGEM_RE.fullmatch(origem):
            raise CanalAquisicaoError(
                "origem_aquisicao_invalida",
                "A origem de aquisição precisa ser uma referência curta.",
            )
    if correlation_id is not None:
        if not isinstance(correlation_id, str):
            raise CanalAquisicaoError(
                "correlation_id_invalido",
                "A correlação de aquisição é inválida.",
            )
        _recusar_material_sensivel(correlation_id, "correlation_id")
        correlation = correlation_id.strip()
        if not _CORRELATION_RE.fullmatch(correlation):
            raise CanalAquisicaoError(
                "correlation_id_invalido",
                "A correlação de aquisição precisa ser uma referência curta.",
            )
    return origem, correlation


def _nome_e_json_estruturado(nome: str) -> bool:
    """O texto inteiro é JSON de objeto ou array, não um nome humano."""
    try:
        valor = json.loads(nome)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False
    return isinstance(valor, (dict, list))


def _nome_tecnico_ou_estruturado(nome: str) -> bool:
    baixo = " ".join(nome.casefold().split())
    if any(marca in baixo for marca in _MARCAS_NOME_TECNICO):
        return True
    rotulo = baixo.replace(": ", ":").replace(" :", ":")
    if "authorization:" in rotulo or "proxy-authorization:" in rotulo:
        return True
    return _nome_e_json_estruturado(nome)


def _nome_somente_digitos_ou_sinais(nome: str) -> bool:
    if not nome:
        return True
    return all(ch.isdigit() or ch.isspace() or ch in _SINAIS_NOME for ch in nome)


def _normalizar_nome(valor: str | None) -> str:
    if not isinstance(valor, str):
        raise CanalAquisicaoError("nome_invalido", "Informe o nome.")
    _recusar_material_sensivel(valor, "nome")
    nome = " ".join(valor.split())
    if (
        not nome
        or len(nome) > 150
        or not any(ch.isalpha() for ch in nome)
        or _nome_tecnico_ou_estruturado(nome)
        or _nome_somente_digitos_ou_sinais(nome)
    ):
        raise CanalAquisicaoError("nome_invalido", "Informe o nome.")
    return nome


def _normalizar_email_cadastro(valor: str | None) -> str:
    if not isinstance(valor, str):
        raise CanalAquisicaoError("email_invalido", "O e-mail informado não é válido.")
    _recusar_material_sensivel(valor, "email")
    from app.auth_services import _normalize_email

    email = _normalize_email(valor)
    if len(email) > 150 or not _EMAIL_RE.fullmatch(email):
        raise CanalAquisicaoError("email_invalido", "O e-mail informado não é válido.")
    return email


def _normalizar_job_role(valor: str | None) -> str:
    if not isinstance(valor, str):
        raise CanalAquisicaoError("cargo_invalido", "O cargo informado não é válido.")
    _recusar_material_sensivel(valor, "job_role")
    cargo = valor.strip()
    if cargo not in _CARGOS:
        raise CanalAquisicaoError("cargo_invalido", "O cargo informado não é válido.")
    return cargo


def _normalizar_referencia_termos(valor: str | None) -> str:
    if not isinstance(valor, str):
        raise CanalAquisicaoError(
            "termos_referencia_invalida",
            "A referência dos termos é inválida.",
        )
    _recusar_material_sensivel(valor, "termos_referencia")
    referencia = valor.strip()
    if not _REFERENCIA_RE.fullmatch(referencia):
        raise CanalAquisicaoError(
            "termos_referencia_invalida",
            "A referência dos termos é inválida.",
        )
    return referencia


def _query_identidade_ativa(provedor: str, sujeito_externo: str) -> IdentidadeCanalExterna | None:
    return (
        IdentidadeCanalExterna.query.filter_by(
            provedor=provedor,
            sujeito_externo=sujeito_externo,
        )
        .filter(IdentidadeCanalExterna.estado != IdentidadeCanalExterna.ESTADO_REVOGADA)
        .one_or_none()
    )


def _buscar_identidade_ativa(provedor: str, sujeito_externo: str) -> IdentidadeCanalExterna | None:
    return _query_identidade_ativa(provedor, sujeito_externo)


def _buscar_identidade_ativa_na_reserva(
    provedor: str,
    sujeito_externo: str,
) -> IdentidadeCanalExterna | None:
    return _buscar_identidade_ativa(provedor, sujeito_externo)


def _buscar_identidade_ativa_apos_colisao(
    provedor: str,
    sujeito_externo: str,
) -> IdentidadeCanalExterna | None:
    return _query_identidade_ativa(provedor, sujeito_externo)


def _buscar_identidade(identidade_id: int) -> IdentidadeCanalExterna:
    identidade = db.session.get(IdentidadeCanalExterna, identidade_id)
    if identidade is None:
        raise CanalAquisicaoError(
            "identidade_nao_encontrada",
            "Identidade externa não encontrada.",
        )
    return identidade


def _recarregar_identidade(identidade_id: int) -> IdentidadeCanalExterna:
    identidade = _buscar_identidade(identidade_id)
    db.session.refresh(identidade)
    return identidade


def _exigir_operavel(identidade: IdentidadeCanalExterna) -> None:
    if identidade.estado not in IdentidadeCanalExterna.ESTADOS_OPERAVEIS_GUEST:
        raise CanalAquisicaoError(
            "identidade_indisponivel",
            "A identidade externa não pode seguir a jornada guest.",
        )


def _exigir_identidade_operavel(identidade_id: int) -> IdentidadeCanalExterna:
    """Relê a identidade no banco antes de avançar, pausar ou retomar."""
    identidade = _buscar_identidade(identidade_id)
    db.session.refresh(identidade)
    _exigir_operavel(identidade)
    return identidade


def _exigir_limite_guest() -> int:
    """Falha antes de qualquer escrita. Não converte float, bool nem texto em inteiro."""
    try:
        valor = limite_interacoes_guest()
    except (TypeError, ValueError) as exc:
        raise CanalAquisicaoError(
            "limite_invalido",
            "O limite de interações guest é inválido.",
        ) from exc
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise CanalAquisicaoError(
            "limite_invalido",
            "O limite de interações guest é inválido.",
        )
    return valor


def _exigir_validade_onboarding():
    """Duração positiva, lida antes de persistir a jornada."""
    try:
        valor = validade_onboarding_canal()
    except (TypeError, ValueError) as exc:
        raise CanalAquisicaoError(
            "validade_invalida",
            "A validade do onboarding por canal é inválida.",
        ) from exc
    if not isinstance(valor, timedelta) or valor <= timedelta(0):
        raise CanalAquisicaoError(
            "validade_invalida",
            "A validade do onboarding por canal é inválida.",
        )
    return valor


def obter_ou_criar_identidade_externa(
    *,
    provedor: str,
    sujeito_externo: str,
    contexto_destino: str | None = None,
    commit: bool = False,
) -> IdentidadeCanalExterna:
    """Resolve a linha ativa de provedor + sujeito externo, ou cria um guest."""
    provedor_ok = _normalizar_provedor(provedor)
    sujeito_ok = _normalizar_sujeito(sujeito_externo, provedor_ok)
    destino_ok = _normalizar_destino(contexto_destino, provedor_ok)
    existente = _buscar_identidade_ativa(provedor_ok, sujeito_ok)
    if existente is not None:
        return existente

    holder: dict[str, IdentidadeCanalExterna | None] = {"identidade": None}

    def _trabalho() -> None:
        novamente = _buscar_identidade_ativa_na_reserva(provedor_ok, sujeito_ok)
        if novamente is not None:
            holder["identidade"] = novamente
            return
        agora = utcnow_naive()
        nova = IdentidadeCanalExterna(
            provedor=provedor_ok,
            sujeito_externo=sujeito_ok,
            contexto_destino=destino_ok,
            estado=IdentidadeCanalExterna.ESTADO_GUEST,
            user_id=None,
            interacoes_uteis=0,
            criada_em=agora,
            atualizada_em=agora,
            vinculada_em=None,
            revogada_em=None,
        )
        db.session.add(nova)
        holder["identidade"] = nova

    try:
        _executar(_trabalho)
    except IdentidadeAtivaDuplicadaError:
        holder["identidade"] = _buscar_identidade_ativa_apos_colisao(provedor_ok, sujeito_ok)
        if holder["identidade"] is None:
            raise
    _finalizar(commit)
    identidade = holder["identidade"]
    if identidade is None or identidade.id is None:
        raise CanalAquisicaoError(
            "identidade_nao_encontrada",
            "Identidade externa não encontrada.",
        )
    return identidade


def _numeros_quota(usadas: int) -> tuple[int, int, int, bool, str]:
    limite = _exigir_limite_guest()
    restantes = max(0, limite - usadas)
    bloqueado = usadas >= limite
    codigo = CODIGO_LIMITE if bloqueado else CODIGO_DISPONIVEL
    return usadas, restantes, limite, bloqueado, codigo


def _estado_guest_de(identidade: IdentidadeCanalExterna) -> EstadoGuest:
    usadas, restantes, limite, bloqueado, codigo = _numeros_quota(int(identidade.interacoes_uteis))
    return EstadoGuest(
        identidade_id=int(identidade.id),
        usadas=usadas,
        restantes=restantes,
        limite=limite,
        bloqueado_para_nova_interacao=bloqueado,
        codigo=codigo,
    )


def obter_estado_guest(identidade_id: int, *, commit: bool = False) -> EstadoGuest:
    identidade = _recarregar_identidade(_exigir_id(identidade_id))
    _finalizar(commit)
    return _estado_guest_de(identidade)


def _buscar_evento(identidade_id: int, evento_externo_id: str) -> InteracaoGuestCanal | None:
    return InteracaoGuestCanal.query.filter_by(
        identidade_id=identidade_id,
        evento_externo_id=evento_externo_id,
    ).one_or_none()


def _buscar_evento_na_reserva(
    identidade_id: int,
    evento_externo_id: str,
) -> InteracaoGuestCanal | None:
    return _buscar_evento(identidade_id, evento_externo_id)


def _buscar_evento_apos_colisao(
    identidade_id: int,
    evento_externo_id: str,
) -> InteracaoGuestCanal | None:
    return _buscar_evento(identidade_id, evento_externo_id)


def _codigo_replay(resultado: str) -> str:
    if resultado == InteracaoGuestCanal.RESULTADO_SUCESSO:
        return CODIGO_INTERACAO_REPLAY
    return CODIGO_EVENTO_REPLAY


def _incrementar_quota_se_houver_vaga(identidade_id: int, agora: datetime) -> bool:
    """Incremento condicional. A linha do UPDATE trava a quota no banco."""
    limite = _exigir_limite_guest()
    linhas = (
        db.session.query(IdentidadeCanalExterna)
        .filter(
            IdentidadeCanalExterna.id == identidade_id,
            IdentidadeCanalExterna.interacoes_uteis < limite,
        )
        .update(
            {
                IdentidadeCanalExterna.interacoes_uteis: IdentidadeCanalExterna.interacoes_uteis + 1,
                IdentidadeCanalExterna.atualizada_em: agora,
            },
            synchronize_session="fetch",
        )
    )
    return linhas == 1


def _registrar_evento_guest(
    identidade_id: int,
    evento_externo_id: str,
    *,
    resultado: str,
    commit: bool,
) -> ResultadoInteracaoGuest:
    identidade_id = _exigir_id(identidade_id)
    evento = _normalizar_evento(evento_externo_id)
    if resultado not in InteracaoGuestCanal.RESULTADOS:
        raise CanalAquisicaoError("resultado_invalido", "O resultado do evento guest é inválido.")
    _exigir_limite_guest()
    _exigir_operavel(_buscar_identidade(identidade_id))
    consumir = resultado == InteracaoGuestCanal.RESULTADO_SUCESSO
    holder: dict[str, str] = {"codigo": ""}

    def _trabalho() -> None:
        atual = _buscar_identidade(identidade_id)
        _exigir_operavel(atual)
        existente = _buscar_evento_na_reserva(atual.id, evento)
        if existente is not None:
            holder["codigo"] = _codigo_replay(existente.resultado)
            return
        agora = utcnow_naive()
        if consumir and not _incrementar_quota_se_houver_vaga(atual.id, agora):
            holder["codigo"] = CODIGO_LIMITE
            return
        if consumir:
            db.session.expire(atual)
        db.session.add(
            InteracaoGuestCanal(
                identidade_id=atual.id,
                evento_externo_id=evento,
                resultado=resultado,
                registrada_em=agora,
            )
        )
        if not consumir:
            atual.atualizada_em = agora
        holder["codigo"] = (
            CODIGO_INTERACAO_REGISTRADA if consumir else CODIGO_EVENTO_SEM_CONSUMO
        )

    try:
        _executar(_trabalho)
    except EventoGuestDuplicadoError:
        existente = _buscar_evento_apos_colisao(identidade_id, evento)
        if existente is None:
            raise
        holder["codigo"] = _codigo_replay(existente.resultado)
    _finalizar(commit)
    identidade = _recarregar_identidade(identidade_id)
    usadas, restantes, limite, bloqueado, _codigo_estado = _numeros_quota(
        int(identidade.interacoes_uteis)
    )
    return ResultadoInteracaoGuest(
        codigo=holder["codigo"],
        usadas=usadas,
        restantes=restantes,
        limite=limite,
        bloqueado_para_nova_interacao=bloqueado,
        consumiu=holder["codigo"] == CODIGO_INTERACAO_REGISTRADA,
    )


def registrar_interacao_guest_concluida(
    identidade_id: int,
    evento_externo_id: str,
    *,
    commit: bool = False,
) -> ResultadoInteracaoGuest:
    """Registra interacao_guest_concluida_com_sucesso, de forma idempotente."""
    return _registrar_evento_guest(
        identidade_id,
        evento_externo_id,
        resultado=InteracaoGuestCanal.RESULTADO_SUCESSO,
        commit=commit,
    )


def registrar_evento_guest_sem_consumo(
    identidade_id: int,
    evento_externo_id: str,
    *,
    motivo: str,
    commit: bool = False,
) -> ResultadoInteracaoGuest:
    """Ocupa a chave do evento sem incrementar a quota."""
    if motivo not in InteracaoGuestCanal.RESULTADOS_SEM_CONSUMO:
        raise CanalAquisicaoError("resultado_invalido", "O resultado do evento guest é inválido.")
    return _registrar_evento_guest(
        identidade_id,
        evento_externo_id,
        resultado=motivo,
        commit=commit,
    )


def _query_jornada_aberta(identidade_id: int) -> OnboardingCanal | None:
    return (
        OnboardingCanal.query.filter_by(identidade_id=identidade_id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .one_or_none()
    )


def _buscar_jornada_aberta(identidade_id: int) -> OnboardingCanal | None:
    return _query_jornada_aberta(identidade_id)


def _buscar_jornada_aberta_apos_colisao(identidade_id: int) -> OnboardingCanal | None:
    """Consulta a jornada vencedora depois do rollback do SAVEPOINT."""
    return _query_jornada_aberta(identidade_id)


def _buscar_jornada_recente(identidade_id: int) -> OnboardingCanal | None:
    return (
        OnboardingCanal.query.filter_by(identidade_id=identidade_id)
        .order_by(OnboardingCanal.id.desc())
        .first()
    )


def _ler_respostas(jornada: OnboardingCanal) -> dict[str, str]:
    bruto = jornada.respostas_entrevista_json
    if not bruto:
        return {}
    try:
        carregado = json.loads(bruto)
    except json.JSONDecodeError as exc:
        raise CanalAquisicaoError(
            "respostas_invalidas",
            "As respostas pendentes da entrevista estão inválidas.",
        ) from exc
    if not isinstance(carregado, dict):
        raise CanalAquisicaoError(
            "respostas_invalidas",
            "As respostas pendentes da entrevista estão inválidas.",
        )
    respostas: dict[str, str] = {}
    for chave, valor in carregado.items():
        if not isinstance(chave, str) or not isinstance(valor, str):
            raise CanalAquisicaoError(
                "respostas_invalidas",
                "As respostas pendentes da entrevista estão inválidas.",
            )
        respostas[chave] = valor
    return respostas


def _serializar_respostas(job_role: str | None, respostas: dict[str, str]) -> str:
    """Persiste só perguntas visíveis na combinação atual. Não aceita chave arbitrária."""
    visiveis = {pergunta.key for pergunta in perguntas_visiveis(job_role, respostas)}
    limpas: dict[str, str] = {}
    for chave, valor in respostas.items():
        if chave not in visiveis:
            raise CanalAquisicaoError(
                "pergunta_inaplicavel",
                "A pergunta complementar não se aplica ao cargo informado.",
            )
        limpas[chave] = valor
    return json.dumps(
        limpas,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _persistir_transicao(
    jornada: OnboardingCanal,
    etapa_esperada: str,
    valores: dict,
) -> bool:
    """Grava a transição só se a etapa no banco ainda for a esperada.

    O WHERE usa a etapa lida antes da decisão. O objeto ORM já carregado
    não autoriza a escrita sozinho.
    """
    jornada_id = int(jornada.id)
    db.session.expire(jornada)
    linhas = (
        db.session.query(OnboardingCanal)
        .filter(
            OnboardingCanal.id == jornada_id,
            OnboardingCanal.etapa == etapa_esperada,
        )
        .update(valores, synchronize_session="fetch")
    )
    return linhas == 1


def _recarregar_jornada(jornada_id: int) -> OnboardingCanal:
    jornada = db.session.get(OnboardingCanal, jornada_id)
    if jornada is None:
        raise CanalAquisicaoError(
            "jornada_ausente",
            "Não há jornada de onboarding para esta identidade.",
        )
    db.session.refresh(jornada)
    return jornada


def _pergunta_atual(jornada: OnboardingCanal) -> str | None:
    if jornada.etapa != OnboardingCanal.ETAPA_ENTREVISTA:
        return None
    proxima = proxima_pergunta(jornada.job_role, _ler_respostas(jornada))
    if proxima is None:
        return None
    return proxima.key


def _montar_onboarding(jornada: OnboardingCanal, codigo: str) -> EstadoOnboardingCanal:
    db.session.refresh(jornada)
    return EstadoOnboardingCanal(
        onboarding_id=int(jornada.id),
        identidade_id=int(jornada.identidade_id),
        etapa_atual=jornada.etapa,
        codigo=codigo,
        pergunta_key=_pergunta_atual(jornada),
        nome=jornada.nome,
        email_normalizado=jornada.email_normalizado,
        job_role=jornada.job_role,
        respostas_entrevista=dict(_ler_respostas(jornada)),
        termos_apresentados=jornada.termos_apresentados_em is not None,
        termos_referencia=jornada.termos_referencia,
        termos_aceitos=jornada.termos_aceitos_em is not None,
        pausas_interacao_guest=int(jornada.pausas_interacao_guest),
        expira_em=jornada.expira_em,
    )


def _codigo_terminal(jornada: OnboardingCanal) -> str:
    if jornada.etapa == OnboardingCanal.ETAPA_EXPIRADO:
        return "jornada_expirada"
    return "jornada_encerrada"


def _expirar_se_vencida(jornada: OnboardingCanal, agora: datetime | None = None) -> bool:
    db.session.refresh(jornada)
    if jornada.etapa in OnboardingCanal.ETAPAS_TERMINAIS:
        return jornada.etapa == OnboardingCanal.ETAPA_EXPIRADO
    momento = agora or utcnow_naive()
    if momento < jornada.expira_em:
        return False
    etapa_esperada = jornada.etapa
    if not _persistir_transicao(
        jornada,
        etapa_esperada,
        {
            "etapa": OnboardingCanal.ETAPA_EXPIRADO,
            "expirada_em": momento,
            "atualizada_em": momento,
        },
    ):
        db.session.refresh(jornada)
        return jornada.etapa == OnboardingCanal.ETAPA_EXPIRADO
    return True


def _email_ja_cadastrado(email: str) -> bool:
    return (
        db.session.query(User.id)
        .filter(func.lower(User.email) == email)
        .first()
        is not None
    )


def _nova_jornada(
    identidade: IdentidadeCanalExterna,
    *,
    origem_aquisicao: str | None,
    correlation_id: str | None,
    agora: datetime,
) -> OnboardingCanal:
    validade = _exigir_validade_onboarding()
    if identidade.estado == IdentidadeCanalExterna.ESTADO_GUEST:
        identidade.estado = IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
        identidade.atualizada_em = agora
    return OnboardingCanal(
        identidade_id=identidade.id,
        etapa=OnboardingCanal.ETAPA_CONVITE,
        pausas_interacao_guest=0,
        taxonomia_versao=TAXONOMIA_VERSAO,
        iniciada_em=agora,
        atualizada_em=agora,
        expira_em=agora + validade,
        origem_aquisicao=origem_aquisicao,
        correlation_id=correlation_id,
    )


def _recusar_etapa(campo: str) -> None:
    raise CanalAquisicaoError(
        "etapa_nao_aceita_campo",
        "O campo informado não avança a etapa atual.",
    )


def _aplicar_entrevista(
    jornada: OnboardingCanal,
    question_key: str | None,
    valor: str | None,
) -> dict:
    """Avança a próxima pergunta ou corrige uma resposta já declarada.

    A correção usa as perguntas visíveis da combinação atual e remove
    respostas que deixaram de se aplicar. A etapa seguinte é recalculada.
    """
    if not isinstance(question_key, str) or not isinstance(valor, str):
        raise CanalAquisicaoError("resposta_invalida", "A resposta da entrevista é inválida.")
    _recusar_material_sensivel(question_key, "question_key")
    _recusar_material_sensivel(valor, "resposta_entrevista")
    chave = question_key.strip()
    resposta = valor.strip()
    entrevista = entrevista_ativa(jornada.job_role)
    if entrevista is None:
        raise CanalAquisicaoError(
            "pergunta_inaplicavel",
            "A pergunta complementar não se aplica ao cargo informado.",
        )
    pergunta = next((item for item in entrevista.perguntas if item.key == chave), None)
    if pergunta is None:
        raise CanalAquisicaoError(
            "pergunta_inaplicavel",
            "A pergunta complementar não se aplica ao cargo informado.",
        )
    if not pergunta.aceita(resposta):
        raise CanalAquisicaoError(
            "resposta_invalida",
            "A resposta informada não é válida para a pergunta atual.",
        )
    atuais = _ler_respostas(jornada)
    esperada = proxima_pergunta(jornada.job_role, atuais)
    correcao = chave in atuais
    avanco = esperada is not None and chave == esperada.key
    if not correcao and not avanco:
        raise CanalAquisicaoError(
            "resposta_invalida",
            "A resposta informada não é válida para a pergunta atual.",
        )
    novas = dict(atuais)
    novas[chave] = resposta
    visiveis = {item.key for item in perguntas_visiveis(jornada.job_role, novas)}
    if chave not in visiveis:
        raise CanalAquisicaoError(
            "combinacao_incoerente",
            "A resposta complementar não se aplica a esta combinação.",
        )
    limpas = {item: novas[item] for item in novas if item in visiveis}
    if proxima_pergunta(jornada.job_role, limpas) is None:
        resultado = validar_combinacao(jornada.job_role, limpas)
        if not resultado.ok:
            raise CanalAquisicaoError(
                resultado.codigo or "combinacao_incoerente",
                resultado.mensagem or "A entrevista complementar está incompleta.",
            )
        etapa = OnboardingCanal.ETAPA_TERMOS
    else:
        etapa = OnboardingCanal.ETAPA_ENTREVISTA
    return {
        "etapa": etapa,
        "respostas_entrevista_json": _serializar_respostas(jornada.job_role, limpas),
    }


def _aplicar_resposta(
    jornada: OnboardingCanal,
    *,
    campo: str,
    valor: str | None,
    question_key: str | None,
    termos_referencia: str | None,
    aceite_declarado: object,
) -> tuple[str, dict]:
    """Calcula o próximo estado sem gravar. A persistência é condicional à etapa."""
    if jornada.etapa == OnboardingCanal.ETAPA_SENHA:
        raise CanalAquisicaoError(
            "senha_nao_aceitada_neste_lote",
            "A senha não é aceita neste lote.",
        )
    if campo == "aceitar_convite":
        if jornada.etapa != OnboardingCanal.ETAPA_CONVITE or valor is not None:
            _recusar_etapa(campo)
        return "resposta_registrada", {"etapa": OnboardingCanal.ETAPA_NOME}
    if campo == "nome":
        if jornada.etapa != OnboardingCanal.ETAPA_NOME:
            _recusar_etapa(campo)
        return "resposta_registrada", {
            "nome": _normalizar_nome(valor),
            "etapa": OnboardingCanal.ETAPA_EMAIL,
        }
    if campo == "email":
        if jornada.etapa != OnboardingCanal.ETAPA_EMAIL:
            _recusar_etapa(campo)
        email = _normalizar_email_cadastro(valor)
        if _email_ja_cadastrado(email):
            return CODIGO_CONTA_EXISTENTE, {
                "email_normalizado": email,
                "etapa": OnboardingCanal.ETAPA_EMAIL,
            }
        return "resposta_registrada", {
            "email_normalizado": email,
            "etapa": OnboardingCanal.ETAPA_CARGO,
        }
    if campo == "job_role":
        if jornada.etapa != OnboardingCanal.ETAPA_CARGO:
            _recusar_etapa(campo)
        cargo = _normalizar_job_role(valor)
        if entrevista_ativa(cargo) is None:
            etapa = OnboardingCanal.ETAPA_TERMOS
        else:
            etapa = OnboardingCanal.ETAPA_ENTREVISTA
        return "resposta_registrada", {"job_role": cargo, "etapa": etapa}
    if campo == "entrevista":
        if jornada.etapa not in (
            OnboardingCanal.ETAPA_ENTREVISTA,
            OnboardingCanal.ETAPA_TERMOS,
        ):
            _recusar_etapa(campo)
        return "resposta_registrada", _aplicar_entrevista(jornada, question_key, valor)
    if campo == "apresentar_termos":
        if jornada.etapa != OnboardingCanal.ETAPA_TERMOS or valor is not None:
            _recusar_etapa(campo)
        referencia = _normalizar_referencia_termos(termos_referencia)
        return "termos_apresentados", {
            "termos_referencia": referencia,
            "termos_apresentados_em": utcnow_naive(),
            "etapa": OnboardingCanal.ETAPA_TERMOS,
        }
    if campo == "declarar_aceite_termos":
        if jornada.etapa != OnboardingCanal.ETAPA_TERMOS or valor is not None:
            raise CanalAquisicaoError(
                "aceite_exige_declaracao_estruturada",
                "O aceite de termos exige declaração estruturada do chamador.",
            )
        if aceite_declarado is not True:
            raise CanalAquisicaoError(
                "aceite_nao_declarado",
                "O aceite de termos não foi declarado.",
            )
        if jornada.termos_apresentados_em is None or not jornada.termos_referencia:
            raise CanalAquisicaoError(
                "termos_nao_apresentados",
                "Os termos ainda não foram apresentados.",
            )
        if termos_referencia is not None and termos_referencia != jornada.termos_referencia:
            raise CanalAquisicaoError(
                "termos_referencia_divergente",
                "A referência dos termos não confere com a apresentada.",
            )
        return "termos_aceitos", {
            "termos_aceitos_em": utcnow_naive(),
            "etapa": OnboardingCanal.ETAPA_SENHA,
        }
    _recusar_etapa(campo)
    return "etapa_nao_aceita_campo", {}


def _preparar_campo_resposta(
    campo: str,
    valor: str | None,
    question_key: str | None,
    termos_referencia: str | None,
) -> str:
    if not isinstance(campo, str):
        raise CanalAquisicaoError("campo_nao_permitido", "O campo informado não faz parte da jornada.")
    campo_ok = campo.strip()
    if campo_ok in _CAMPOS_PROIBIDOS:
        raise CanalAquisicaoError(
            "senha_nao_aceitada_neste_lote",
            "A senha não é aceita neste lote.",
        )
    if campo_ok not in _CAMPOS_RESPOSTA:
        raise CanalAquisicaoError("campo_nao_permitido", "O campo informado não faz parte da jornada.")
    for texto_campo, nome in (
        (valor, "valor"),
        (question_key, "question_key"),
        (termos_referencia, "termos_referencia"),
    ):
        if isinstance(texto_campo, str):
            _recusar_material_sensivel(texto_campo, nome)
    return campo_ok


def iniciar_onboarding_canal(
    identidade_id: int,
    *,
    origem_aquisicao: str | None = None,
    correlation_id: str | None = None,
    commit: bool = False,
) -> EstadoOnboardingCanal:
    """Abre a jornada em convite_cadastro. Não reinicia uma jornada já existente."""
    identidade_id = _exigir_id(identidade_id)
    origem, correlation = _normalizar_aquisicao(origem_aquisicao, correlation_id)
    _exigir_identidade_operavel(identidade_id)
    _exigir_validade_onboarding()
    holder: dict[str, OnboardingCanal | str | None] = {"jornada": None, "codigo": None}

    def _trabalho() -> None:
        identidade = _buscar_identidade(identidade_id)
        _exigir_operavel(identidade)
        aberta = _buscar_jornada_aberta(identidade_id)
        if aberta is not None:
            if _expirar_se_vencida(aberta):
                holder["jornada"] = aberta
                holder["codigo"] = "jornada_expirada"
                return
            holder["jornada"] = aberta
            holder["codigo"] = "onboarding_em_andamento"
            return
        recente = _buscar_jornada_recente(identidade_id)
        if recente is not None:
            holder["jornada"] = recente
            holder["codigo"] = _codigo_terminal(recente)
            return
        agora = utcnow_naive()
        jornada = _nova_jornada(
            identidade,
            origem_aquisicao=origem,
            correlation_id=correlation,
            agora=agora,
        )
        db.session.add(jornada)
        holder["jornada"] = jornada
        holder["codigo"] = "onboarding_iniciado"

    try:
        _executar(_trabalho)
    except JornadaAbertaDuplicadaError:
        vencedora = _buscar_jornada_aberta_apos_colisao(identidade_id)
        if vencedora is None:
            raise
        holder["jornada"] = vencedora
        holder["codigo"] = "onboarding_em_andamento"
    _finalizar(commit)
    jornada = holder["jornada"]
    codigo = holder["codigo"]
    if not isinstance(jornada, OnboardingCanal) or not isinstance(codigo, str):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, codigo)


def registrar_resposta_onboarding(
    identidade_id: int,
    *,
    campo: str,
    valor: str | None = None,
    question_key: str | None = None,
    termos_referencia: str | None = None,
    aceite_declarado: bool = False,
    commit: bool = False,
) -> EstadoOnboardingCanal:
    identidade_id = _exigir_id(identidade_id)
    campo_ok = _preparar_campo_resposta(campo, valor, question_key, termos_referencia)
    _exigir_identidade_operavel(identidade_id)
    holder: dict[str, OnboardingCanal | str | None] = {"jornada": None, "codigo": None}

    def _vencer() -> None:
        _exigir_identidade_operavel(identidade_id)
        aberta = _buscar_jornada_aberta(identidade_id)
        if aberta is not None and _expirar_se_vencida(aberta):
            holder["jornada"] = aberta
            holder["codigo"] = "jornada_expirada"

    _executar(_vencer)
    if holder["codigo"] == "jornada_expirada" and isinstance(holder["jornada"], OnboardingCanal):
        _finalizar(commit)
        return _montar_onboarding(holder["jornada"], "jornada_expirada")

    def _trabalho() -> None:
        _exigir_identidade_operavel(identidade_id)
        jornada = _buscar_jornada_aberta(identidade_id)
        if jornada is None:
            recente = _buscar_jornada_recente(identidade_id)
            if recente is None:
                raise CanalAquisicaoError(
                    "jornada_ausente",
                    "Não há jornada de onboarding para esta identidade.",
                )
            holder["jornada"] = recente
            holder["codigo"] = _codigo_terminal(recente)
            return
        db.session.refresh(jornada)
        etapa_esperada = jornada.etapa
        codigo, valores = _aplicar_resposta(
            jornada,
            campo=campo_ok,
            valor=valor,
            question_key=question_key,
            termos_referencia=termos_referencia,
            aceite_declarado=aceite_declarado,
        )
        valores["atualizada_em"] = utcnow_naive()
        jornada_id = int(jornada.id)
        if not _persistir_transicao(jornada, etapa_esperada, valores):
            holder["jornada"] = _recarregar_jornada(jornada_id)
            holder["codigo"] = CODIGO_TRANSICAO_CONFLITO
            return
        holder["jornada"] = _recarregar_jornada(jornada_id)
        holder["codigo"] = codigo

    _executar(_trabalho)
    _finalizar(commit)
    jornada = holder["jornada"]
    codigo = holder["codigo"]
    if not isinstance(jornada, OnboardingCanal) or not isinstance(codigo, str):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, codigo)


def obter_proxima_etapa(identidade_id: int, *, commit: bool = False) -> EstadoOnboardingCanal:
    identidade_id = _exigir_id(identidade_id)
    holder: dict[str, OnboardingCanal | str | None] = {"jornada": None, "codigo": None}

    def _trabalho() -> None:
        aberta = _buscar_jornada_aberta(identidade_id)
        if aberta is None:
            recente = _buscar_jornada_recente(identidade_id)
            if recente is None:
                raise CanalAquisicaoError(
                    "jornada_ausente",
                    "Não há jornada de onboarding para esta identidade.",
                )
            holder["jornada"] = recente
            holder["codigo"] = _codigo_terminal(recente)
            return
        if _expirar_se_vencida(aberta):
            holder["jornada"] = aberta
            holder["codigo"] = "jornada_expirada"
            return
        holder["jornada"] = aberta
        if (
            aberta.etapa == OnboardingCanal.ETAPA_EMAIL
            and aberta.email_normalizado
            and _email_ja_cadastrado(aberta.email_normalizado)
        ):
            holder["codigo"] = CODIGO_CONTA_EXISTENTE
            return
        holder["codigo"] = "em_andamento"

    _executar(_trabalho)
    _finalizar(commit)
    jornada = holder["jornada"]
    codigo = holder["codigo"]
    if not isinstance(jornada, OnboardingCanal) or not isinstance(codigo, str):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, codigo)


def pausar_para_interacao_guest(
    identidade_id: int,
    *,
    commit: bool = False,
) -> EstadoOnboardingCanal:
    """Registra a pausa e mantém a etapa. Não consome quota e não avança cadastro."""
    identidade_id = _exigir_id(identidade_id)
    _exigir_identidade_operavel(identidade_id)
    holder: dict[str, OnboardingCanal | str | None] = {"jornada": None, "codigo": None}

    def _trabalho() -> None:
        _exigir_identidade_operavel(identidade_id)
        aberta = _buscar_jornada_aberta(identidade_id)
        if aberta is None:
            recente = _buscar_jornada_recente(identidade_id)
            if recente is None:
                raise CanalAquisicaoError(
                    "jornada_ausente",
                    "Não há jornada de onboarding para esta identidade.",
                )
            holder["jornada"] = recente
            holder["codigo"] = _codigo_terminal(recente)
            return
        if _expirar_se_vencida(aberta):
            holder["jornada"] = aberta
            holder["codigo"] = "jornada_expirada"
            return
        db.session.refresh(aberta)
        etapa_esperada = aberta.etapa
        pausas = int(aberta.pausas_interacao_guest) + 1
        jornada_id = int(aberta.id)
        if not _persistir_transicao(
            aberta,
            etapa_esperada,
            {
                "pausas_interacao_guest": pausas,
                "atualizada_em": utcnow_naive(),
            },
        ):
            holder["jornada"] = _recarregar_jornada(jornada_id)
            holder["codigo"] = CODIGO_TRANSICAO_CONFLITO
            return
        holder["jornada"] = _recarregar_jornada(jornada_id)
        holder["codigo"] = "pausada_para_interacao_guest"

    _executar(_trabalho)
    _finalizar(commit)
    jornada = holder["jornada"]
    codigo = holder["codigo"]
    if not isinstance(jornada, OnboardingCanal) or not isinstance(codigo, str):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, codigo)


def retomar_onboarding(identidade_id: int, *, commit: bool = False) -> EstadoOnboardingCanal:
    identidade_id = _exigir_id(identidade_id)
    _exigir_identidade_operavel(identidade_id)
    holder: dict[str, OnboardingCanal | str | None] = {"jornada": None, "codigo": None}

    def _trabalho() -> None:
        _exigir_identidade_operavel(identidade_id)
        aberta = _buscar_jornada_aberta(identidade_id)
        if aberta is None:
            recente = _buscar_jornada_recente(identidade_id)
            if recente is None:
                raise CanalAquisicaoError(
                    "jornada_ausente",
                    "Não há jornada de onboarding para esta identidade.",
                )
            holder["jornada"] = recente
            holder["codigo"] = _codigo_terminal(recente)
            return
        if _expirar_se_vencida(aberta):
            holder["jornada"] = aberta
            holder["codigo"] = "jornada_expirada"
            return
        holder["jornada"] = aberta
        holder["codigo"] = "jornada_retomada"

    _executar(_trabalho)
    _finalizar(commit)
    jornada = holder["jornada"]
    codigo = holder["codigo"]
    if not isinstance(jornada, OnboardingCanal) or not isinstance(codigo, str):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, codigo)


def reiniciar_onboarding_canal(
    identidade_id: int,
    *,
    confirmar: bool = False,
    origem_aquisicao: str | None = None,
    correlation_id: str | None = None,
    commit: bool = False,
) -> EstadoOnboardingCanal:
    """Encerra a jornada atual e abre outra. Exige confirmação explícita."""
    if confirmar is not True:
        raise CanalAquisicaoError(
            "reinicio_nao_confirmado",
            "O reinício da jornada exige confirmação explícita.",
        )
    identidade_id = _exigir_id(identidade_id)
    origem, correlation = _normalizar_aquisicao(origem_aquisicao, correlation_id)
    _exigir_identidade_operavel(identidade_id)
    _exigir_validade_onboarding()
    holder: dict[str, OnboardingCanal | None] = {"jornada": None}

    def _trabalho() -> None:
        identidade = _buscar_identidade(identidade_id)
        _exigir_operavel(identidade)
        aberta = _buscar_jornada_aberta(identidade_id)
        recente = _buscar_jornada_recente(identidade_id)
        if aberta is None and recente is None:
            raise CanalAquisicaoError(
                "jornada_ausente",
                "Não há jornada de onboarding para esta identidade.",
            )
        agora = utcnow_naive()
        if aberta is not None and not _expirar_se_vencida(aberta, agora):
            aberta.etapa = OnboardingCanal.ETAPA_CANCELADO
            aberta.cancelada_em = agora
            aberta.atualizada_em = agora
        if aberta is not None:
            db.session.flush()
        jornada = _nova_jornada(
            identidade,
            origem_aquisicao=origem,
            correlation_id=correlation,
            agora=agora,
        )
        db.session.add(jornada)
        holder["jornada"] = jornada

    _executar(_trabalho)
    _finalizar(commit)
    jornada = holder["jornada"]
    if not isinstance(jornada, OnboardingCanal):
        raise CanalAquisicaoError("jornada_ausente", "Não há jornada de onboarding para esta identidade.")
    return _montar_onboarding(jornada, "onboarding_reiniciado")
