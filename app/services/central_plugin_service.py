"""
Domínio da Central de Plugins (SCRUM-222).

Lote 1 registra catálogo homologado, capability, conexão e restrição.
Lote 2 decide a autorização efetiva. Não chama provider, não executa
capability e não guarda segredo.
Lote 3 altera o catálogo (metadados, status, titularidade e governança
da capability). Essas funções só persistem. Não chamam provider, não
executam capability e não substituem avaliar_autorizacao_plugin.

A ausência de PluginRestricaoUsuario não é permissão. A ausência de
PluginConcessaoProvedor não é concessão. Só avaliar_autorizacao_plugin
resolve a interseção. A identidade vem do contexto explícito: não há
leitura de request, sessão, current_user nem usuário interno implícito.

Unidade transacional: cada escrita que pode falhar por integridade entra num
SAVEPOINT já aberto. No sqlite3 em controle legado, a unidade abre BEGIN antes
desse SAVEPOINT; sem isso, RELEASE confirma a escrita mesmo com commit=False.
Session.begin_nested() faz flush do pendente antes de abrir o SAVEPOINT; por
isso a unidade não pode ser adicionada antes.

Garantias deste lote:
- coerência titularidade/conta_id da própria conexão: banco;
- titularidade compatível com o Plugin: este serviço. Escrita direta fora
  do domínio não é API suportada.
- PluginEventoCentral é append-only neste serviço (só INSERT). Não há
  trava física no banco.
- usuario_originador_id em PluginConexao é o criador da conexão.
  O originador de uma ação posterior fica no evento correspondente.
- avaliar_autorizacao_plugin não confirma pendência alheia: commit padrão
  é False, e commit True só alcança a sessão se um evento novo foi inserido.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Conta,
    Plugin,
    PluginCapability,
    PluginConcessaoProvedor,
    PluginConexao,
    PluginCredencialReferencia,
    PluginEventoCentral,
    PluginRestricaoUsuario,
    User,
    utcnow_naive,
)

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CHAVE_RE = re.compile(r"^[a-z0-9]+(?:[._][a-z0-9]+)*$")
_COFRE_REF_RE = re.compile(r"^[a-z0-9]+(?:[._:-][a-z0-9]+)*$")
# Identificador técnico curto. Não é JSON, header, atribuição nem segredo.
_DIAGNOSTICO_CODIGO_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DIAGNOSTICO_CODIGO_MAX = PluginConexao.__table__.c.diagnostico_codigo.type.length
_DIAGNOSTICO_RESUMO_MAX = PluginConexao.__table__.c.diagnostico_resumo.type.length
# Frase de apresentação: letras, dígitos e pontuação simples, com espaço.
_DIAGNOSTICO_RESUMO_RE = re.compile(
    r"^(?=.*\s)[0-9A-Za-z\u00C0-\u024F .,;:!?()\-]{1,255}$"
)
# Trecho contínuo longo demais para texto de apresentação.
_TRECHO_OPACO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-){15,}")
# Formato de credencial. Não é taxonomia de diagnóstico.
_PREFIXO_CREDENCIAL_RE = re.compile(
    r"(?i)(?:^|\s)(?:sk-|sk_|pk-|pk_|rk-|rk_|eyj|ya29|bearer\b)"
)
_IDENTIFICADOR_EXTERNO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,119}$")
_CANAL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
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
)

UQ_PLUGIN_SLUG = "uq_plugin_slug"
UQ_CAPABILITY_CHAVE = "uq_plugin_capability_plugin_chave"
UQ_CONEXAO_ATIVA = "uq_plugin_conexao_user_plugin_ativa"
UQ_RESTRICAO = "uq_plugin_restricao_conexao_capability"
UQ_CONCESSAO = "uq_plugin_concessao_conexao_capability"

_SQLITE_UNIQUE = (
    ("UNIQUE constraint failed: plugin.slug", UQ_PLUGIN_SLUG),
    (f"UNIQUE constraint failed: {UQ_PLUGIN_SLUG}", UQ_PLUGIN_SLUG),
    (
        "UNIQUE constraint failed: plugin_capability.plugin_id, plugin_capability.chave",
        UQ_CAPABILITY_CHAVE,
    ),
    (f"UNIQUE constraint failed: {UQ_CAPABILITY_CHAVE}", UQ_CAPABILITY_CHAVE),
    (
        "UNIQUE constraint failed: plugin_conexao.user_id, plugin_conexao.plugin_id",
        UQ_CONEXAO_ATIVA,
    ),
    (f"UNIQUE constraint failed: {UQ_CONEXAO_ATIVA}", UQ_CONEXAO_ATIVA),
    (
        "UNIQUE constraint failed: plugin_restricao_usuario.conexao_id, plugin_restricao_usuario.capability_id",
        UQ_RESTRICAO,
    ),
    (f"UNIQUE constraint failed: {UQ_RESTRICAO}", UQ_RESTRICAO),
    (
        "UNIQUE constraint failed: plugin_concessao_provedor.conexao_id, plugin_concessao_provedor.capability_id",
        UQ_CONCESSAO,
    ),
    (f"UNIQUE constraint failed: {UQ_CONCESSAO}", UQ_CONCESSAO),
)

_CAMPOS_PUBLICOS_CONEXAO = (
    "id",
    "plugin_id",
    "user_id",
    "conta_id",
    "usuario_originador_id",
    "titularidade",
    "identificador_externo",
    "estado",
    "diagnostico_codigo",
    "diagnostico_resumo",
    "created_at",
    "updated_at",
    "estado_alterado_em",
    "desconectado_em",
    "revogado_em",
)


class CentralPluginError(ValueError):
    """Erro de domínio da Central de Plugins."""


class PluginInvalidoError(CentralPluginError):
    """Plugin ausente, fora do catálogo ou com dados inválidos."""


class PluginSlugDuplicadoError(CentralPluginError):
    """Slug estável já usado por outro plugin."""


class CapabilityInvalidaError(CentralPluginError):
    """Capability ausente ou incompatível com o plugin."""


class CapabilityChaveDuplicadaError(CentralPluginError):
    """Chave de capability já registrada neste plugin."""


class TitularidadeNaoPermitidaError(CentralPluginError):
    """Titularidade pedida não é das suportadas pelo plugin, ou está inconsistente."""


class ConexaoAtivaDuplicadaError(CentralPluginError):
    """O usuário já ocupa o slot ativo deste plugin."""


class ConexaoNaoEncontradaError(CentralPluginError):
    """Conexão inexistente para o critério pedido."""


class EstadoConexaoInvalidoError(CentralPluginError):
    """Estado interno desconhecido ou transição incompatível com revogação."""


class RestricaoUsuarioInvalidaError(CentralPluginError):
    """Restrição não pertence ao usuário/conexão ou a capability não foi liberada."""


class MaterialSensivelRecusadoError(CentralPluginError):
    """Texto recusado para não persistir segredo ou payload de provider."""


class ConcessaoProvedorInvalidaError(CentralPluginError):
    """Concessão interna incompatível com a conexão ou com o resultado fechado."""


def identificar_constraint_integrity_error(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    nome_pg = getattr(diag, "constraint_name", None) if diag is not None else None
    if nome_pg:
        nome = str(nome_pg)
        reconhecidas = {
            UQ_PLUGIN_SLUG,
            UQ_CAPABILITY_CHAVE,
            UQ_CONEXAO_ATIVA,
            UQ_RESTRICAO,
            UQ_CONCESSAO,
        }
        return nome if nome in reconhecidas else None
    msg = str(orig if orig is not None else exc)
    for fragmento, nome in _SQLITE_UNIQUE:
        if fragmento in msg:
            return nome
    return None


def converter_ou_relancar_integrity_error(exc: IntegrityError) -> None:
    nome = identificar_constraint_integrity_error(exc)
    if nome == UQ_PLUGIN_SLUG:
        raise PluginSlugDuplicadoError("Já existe plugin com este slug.") from exc
    if nome == UQ_CAPABILITY_CHAVE:
        raise CapabilityChaveDuplicadaError(
            "Já existe capability com esta chave neste plugin."
        ) from exc
    if nome == UQ_CONEXAO_ATIVA:
        raise ConexaoAtivaDuplicadaError(
            "Este usuário já possui conexão ativa para este plugin."
        ) from exc
    if nome == UQ_RESTRICAO:
        raise RestricaoUsuarioInvalidaError(
            "Já existe restrição desta capability nesta conexão."
        ) from exc
    if nome == UQ_CONCESSAO:
        raise ConcessaoProvedorInvalidaError(
            "Já existe concessão desta capability nesta conexão."
        ) from exc
    raise exc


def _garantir_transacao_externa_sqlite() -> None:
    """Abre BEGIN antes do SAVEPOINT no sqlite3 em controle legado.

    Nesse modo, SAVEPOINT não inicia transação. RELEASE SAVEPOINT confirma a
    escrita na hora, e o rollback da sessão não a desfaz. commit=False deixaria
    de ser reversível. Se o sqlite já está numa transação, o SAVEPOINT entra
    nela. Outros dialetos já mantêm o SAVEPOINT dentro da transação da sessão.
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
    """Persiste a unidade lógica dentro de um SAVEPOINT já aberto.

    O flush automático de Session.begin_nested() ocorre antes do SAVEPOINT.
    Objetos novos e mutações desta unidade precisam nascer dentro do bloco.
    IntegrityError reverte só o SAVEPOINT. A transação externa, inclusive
    alterações anteriores do chamador com commit=False, permanece utilizável.
    Qualquer outra falha também reverte o SAVEPOINT antes de propagar.
    """
    _garantir_transacao_externa_sqlite()
    try:
        with db.session.begin_nested():
            yield
            db.session.flush()
    except IntegrityError as exc:
        converter_ou_relancar_integrity_error(exc)


def _finalizar(commit: bool) -> None:
    if commit:
        db.session.commit()


def _recusar_material_sensivel(valor: str, campo: str) -> None:
    baixo = valor.casefold()
    if any(marca in baixo for marca in _MARCAS_SECRETAS):
        raise MaterialSensivelRecusadoError(
            f"{campo} não pode carregar material secreto ou payload de credencial."
        )


def _normalizar_slug(slug: str) -> str:
    texto = (slug or "").strip().lower()
    if not _SLUG_RE.fullmatch(texto) or len(texto) > 80:
        raise PluginInvalidoError("Slug de plugin inválido.")
    return texto


def _normalizar_chave(chave: str, *, erro: type[CentralPluginError]) -> str:
    texto = (chave or "").strip().lower()
    if not _CHAVE_RE.fullmatch(texto) or len(texto) > 80:
        raise erro("Chave inválida.")
    return texto


def _texto_limitado(
    valor: str | None,
    limite: int,
    *,
    obrigatorio: bool,
    campo: str,
    erro: type[CentralPluginError] = PluginInvalidoError,
) -> str | None:
    if valor is None:
        if obrigatorio:
            raise erro(f"{campo} é obrigatório.")
        return None
    texto = str(valor).strip()
    if not texto:
        if obrigatorio:
            raise erro(f"{campo} é obrigatório.")
        return None
    if len(texto) > limite:
        raise erro(f"{campo} excede {limite} caracteres.")
    _recusar_material_sensivel(texto, campo)
    return texto


def _usuario_existente(user_id: int) -> User:
    user = db.session.get(User, int(user_id))
    if user is None:
        raise PluginInvalidoError("Usuário inexistente.")
    return user


def _plugin_existente(plugin_id: int) -> Plugin:
    plugin = db.session.get(Plugin, int(plugin_id))
    if plugin is None:
        raise PluginInvalidoError("Plugin inexistente.")
    return plugin


def _tipo_evento_de_estado(estado_novo: str) -> str:
    if estado_novo == PluginConexao.ESTADO_BLOQUEADO:
        return PluginEventoCentral.TIPO_BLOQUEADO
    if estado_novo == PluginConexao.ESTADO_DESABILITADO:
        return PluginEventoCentral.TIPO_DESABILITADO
    if estado_novo == PluginConexao.ESTADO_DESCONECTADO:
        return PluginEventoCentral.TIPO_DESCONECTADO
    return PluginEventoCentral.TIPO_CONEXAO_ESTADO_ALTERADO


def _registrar_evento(
    *,
    tipo_evento: str,
    detalhe_codigo: str,
    conexao: PluginConexao,
    usuario_originador_id: int,
    capability_id: int | None = None,
    estado_anterior: str | None = None,
    estado_novo: str | None = None,
) -> PluginEventoCentral:
    """Único caminho de escrita de evento neste serviço. Só INSERT.

    usuario_originador_id aqui é quem originou este evento, não o criador
    gravado em PluginConexao.
    """
    evento = PluginEventoCentral(
        tipo_evento=tipo_evento,
        detalhe_codigo=detalhe_codigo,
        conexao_id=conexao.id,
        plugin_id=conexao.plugin_id,
        capability_id=capability_id,
        user_id=conexao.user_id,
        usuario_originador_id=int(usuario_originador_id),
        conta_id=conexao.conta_id,
        estado_anterior=estado_anterior,
        estado_novo=estado_novo,
        created_at=utcnow_naive(),
    )
    db.session.add(evento)
    return evento


def registrar_plugin(
    *,
    slug: str,
    nome: str,
    adapter_key: str,
    suporta_titularidade_pessoal: bool,
    suporta_titularidade_corporativa: bool,
    descricao: str | None = None,
    status: str = Plugin.STATUS_RASCUNHO,
    criado_por_user_id: int | None = None,
    commit: bool = True,
) -> Plugin:
    if status not in Plugin.STATUSES:
        raise PluginInvalidoError("Status de publicação inválido.")
    if not suporta_titularidade_pessoal and not suporta_titularidade_corporativa:
        raise PluginInvalidoError("O plugin precisa declarar ao menos uma titularidade.")
    if criado_por_user_id is not None:
        _usuario_existente(criado_por_user_id)
    plugin = Plugin(
        slug=_normalizar_slug(slug),
        nome=_texto_limitado(nome, 255, obrigatorio=True, campo="nome") or "",
        descricao=_texto_limitado(descricao, 2000, obrigatorio=False, campo="descricao"),
        status=status,
        suporta_titularidade_pessoal=bool(suporta_titularidade_pessoal),
        suporta_titularidade_corporativa=bool(suporta_titularidade_corporativa),
        adapter_key=_normalizar_chave(adapter_key, erro=PluginInvalidoError),
        criado_por_user_id=int(criado_por_user_id) if criado_por_user_id is not None else None,
        created_at=utcnow_naive(),
        updated_at=utcnow_naive(),
    )
    with _unidade_transacional():
        db.session.add(plugin)
    _finalizar(commit)
    return plugin


def listar_plugins(*, status: str | None = None) -> list[Plugin]:
    consulta = Plugin.query
    if status is not None:
        if status not in Plugin.STATUSES:
            raise PluginInvalidoError("Status de publicação inválido.")
        consulta = consulta.filter_by(status=status)
    return consulta.order_by(Plugin.slug.asc()).all()


def registrar_capability(
    *,
    plugin_id: int,
    chave: str,
    nome: str,
    politica_maxima: str,
    natureza: str,
    descricao: str | None = None,
    status: str = PluginCapability.STATUS_DISPONIVEL,
    commit: bool = True,
) -> PluginCapability:
    plugin = _plugin_existente(plugin_id)
    if status not in PluginCapability.STATUSES:
        raise CapabilityInvalidaError("Status de capability inválido.")
    if politica_maxima not in PluginCapability.POLITICAS:
        raise CapabilityInvalidaError("Política máxima inválida.")
    if natureza not in PluginCapability.NATUREZAS:
        raise CapabilityInvalidaError("Natureza da capability inválida.")
    capability = PluginCapability(
        plugin_id=plugin.id,
        chave=_normalizar_chave(chave, erro=CapabilityInvalidaError),
        nome=_texto_limitado(
            nome, 255, obrigatorio=True, campo="nome", erro=CapabilityInvalidaError
        )
        or "",
        descricao=_texto_limitado(
            descricao, 2000, obrigatorio=False, campo="descricao", erro=CapabilityInvalidaError
        ),
        status=status,
        politica_maxima=politica_maxima,
        natureza=natureza,
        created_at=utcnow_naive(),
        updated_at=utcnow_naive(),
    )
    with _unidade_transacional():
        db.session.add(capability)
    _finalizar(commit)
    return capability


def listar_capabilities(plugin_id: int) -> list[PluginCapability]:
    _plugin_existente(plugin_id)
    return (
        PluginCapability.query.filter_by(plugin_id=int(plugin_id))
        .order_by(PluginCapability.chave.asc())
        .all()
    )


def atualizar_metadados_plugin(
    plugin_id: int,
    *,
    nome: str,
    descricao: str | None,
    adapter_key: str,
    commit: bool = True,
) -> Plugin:
    """Atualiza nome, descrição e adapter key. O slug permanece estável.

    Não altera status, titularidade, conexões nem capability.
    """
    plugin = _plugin_existente(plugin_id)
    nome_n = _texto_limitado(nome, 255, obrigatorio=True, campo="nome") or ""
    descricao_n = _texto_limitado(descricao, 2000, obrigatorio=False, campo="descricao")
    adapter_n = _normalizar_chave(adapter_key, erro=PluginInvalidoError)
    if (
        plugin.nome == nome_n
        and plugin.descricao == descricao_n
        and plugin.adapter_key == adapter_n
    ):
        return plugin
    with _unidade_transacional():
        plugin.nome = nome_n
        plugin.descricao = descricao_n
        plugin.adapter_key = adapter_n
        plugin.updated_at = utcnow_naive()
        db.session.add(plugin)
    _finalizar(commit)
    return plugin


def definir_status_plugin(plugin_id: int, status: str, *, commit: bool = True) -> Plugin:
    """Altera só o status do catálogo.

    Não desconecta usuários, não reescreve linhas de conexão e não chama provider.
    """
    if status not in Plugin.STATUSES:
        raise PluginInvalidoError("Status de publicação inválido.")
    plugin = _plugin_existente(plugin_id)
    if plugin.status == status:
        return plugin
    with _unidade_transacional():
        plugin.status = status
        plugin.updated_at = utcnow_naive()
        db.session.add(plugin)
    _finalizar(commit)
    return plugin


def definir_titularidade_plugin(
    plugin_id: int,
    *,
    suporta_titularidade_pessoal: bool,
    suporta_titularidade_corporativa: bool,
    commit: bool = True,
) -> Plugin:
    """Declara titularidades suportadas. Não reescreve conexões já registradas."""
    if not suporta_titularidade_pessoal and not suporta_titularidade_corporativa:
        raise PluginInvalidoError("O plugin precisa declarar ao menos uma titularidade.")
    plugin = _plugin_existente(plugin_id)
    pessoal = bool(suporta_titularidade_pessoal)
    corporativa = bool(suporta_titularidade_corporativa)
    if (
        bool(plugin.suporta_titularidade_pessoal) is pessoal
        and bool(plugin.suporta_titularidade_corporativa) is corporativa
    ):
        return plugin
    with _unidade_transacional():
        plugin.suporta_titularidade_pessoal = pessoal
        plugin.suporta_titularidade_corporativa = corporativa
        plugin.updated_at = utcnow_naive()
        db.session.add(plugin)
    _finalizar(commit)
    return plugin


def atualizar_governanca_capability(
    capability_id: int,
    *,
    plugin_id: int,
    politica_maxima: str,
    status: str,
    commit: bool = True,
) -> PluginCapability:
    """Persiste teto e status da capability. Não reavalia autorização."""
    if status not in PluginCapability.STATUSES:
        raise CapabilityInvalidaError("Status de capability inválido.")
    if politica_maxima not in PluginCapability.POLITICAS:
        raise CapabilityInvalidaError("Política máxima inválida.")
    capability = db.session.get(PluginCapability, int(capability_id))
    if capability is None or capability.plugin_id != int(plugin_id):
        raise CapabilityInvalidaError("Capability inexistente neste plugin.")
    if capability.politica_maxima == politica_maxima and capability.status == status:
        return capability
    with _unidade_transacional():
        capability.politica_maxima = politica_maxima
        capability.status = status
        capability.updated_at = utcnow_naive()
        db.session.add(capability)
    _finalizar(commit)
    return capability


def obter_conexao_usuario(user_id: int, plugin_id: int) -> PluginConexao | None:
    """Devolve a conexão que ocupa o slot, se houver. Não cria conexão."""
    return (
        PluginConexao.query.filter(
            PluginConexao.user_id == int(user_id),
            PluginConexao.plugin_id == int(plugin_id),
            PluginConexao.estado.in_(PluginConexao.ESTADOS_QUE_OCUPAM_SLOT),
        )
        .order_by(PluginConexao.id.asc())
        .first()
    )


def listar_conexoes_para_revogacao(user_id: int) -> list[PluginConexao]:
    """
    Localiza pelo user_id as conexões que ainda ocupam slot.

    Não revoga, não altera Multiuser e não apaga histórico desconectado.
    A revogação futura pode percorrer esta lista e marcar cada linha.
    """
    return (
        PluginConexao.query.filter(
            PluginConexao.user_id == int(user_id),
            PluginConexao.estado.in_(PluginConexao.ESTADOS_QUE_OCUPAM_SLOT),
        )
        .order_by(PluginConexao.id.asc())
        .all()
    )


def listar_conexoes_do_usuario(user_id: int) -> list[PluginConexao]:
    """Todas as linhas do usuário, inclusive desconectadas, para auditoria direta."""
    return (
        PluginConexao.query.filter_by(user_id=int(user_id))
        .order_by(PluginConexao.id.asc())
        .all()
    )


def _validar_titularidade(
    plugin: Plugin,
    *,
    titularidade: str,
    conta_id: int | None,
) -> int | None:
    """Compatibilidade Plugin × conexão. O banco não replica esta regra.

    O banco garante só a forma da linha: pessoal sem conta_id, corporativa
    com conta_id. INSERT direto fora deste serviço não é API suportada.
    """
    if titularidade not in PluginConexao.TITULARIDADES:
        raise TitularidadeNaoPermitidaError("Titularidade desconhecida.")
    if not plugin.aceita_titularidade(titularidade):
        raise TitularidadeNaoPermitidaError(
            "Este plugin não suporta a titularidade solicitada."
        )
    if titularidade == PluginConexao.TITULARIDADE_PESSOAL:
        if conta_id is not None:
            raise TitularidadeNaoPermitidaError(
                "Conexão pessoal não tem Conta proprietária."
            )
        return None
    if conta_id is None:
        raise TitularidadeNaoPermitidaError(
            "Conexão corporativa exige a Conta proprietária."
        )
    conta = db.session.get(Conta, int(conta_id))
    if conta is None:
        raise TitularidadeNaoPermitidaError("Conta proprietária inexistente.")
    return conta.id


def _validar_diagnostico_codigo(valor: str) -> str:
    """Código técnico curto, no limite da coluna. Não é payload nem segredo."""
    texto = valor.strip().lower()
    if len(texto) > _DIAGNOSTICO_CODIGO_MAX:
        raise MaterialSensivelRecusadoError(
            f"Código de diagnóstico excede {_DIAGNOSTICO_CODIGO_MAX} caracteres."
        )
    if not _DIAGNOSTICO_CODIGO_RE.fullmatch(texto) or _PREFIXO_CREDENCIAL_RE.match(texto):
        raise MaterialSensivelRecusadoError(
            "Código de diagnóstico deve ser um identificador técnico curto."
        )
    return texto


def _validar_diagnostico_resumo(valor: str) -> str:
    """Texto de apresentação. Recusa JSON, atribuição, header e token opaco."""
    texto = valor.strip()
    if (
        len(texto) > _DIAGNOSTICO_RESUMO_MAX
        or not _DIAGNOSTICO_RESUMO_RE.fullmatch(texto)
        or _PREFIXO_CREDENCIAL_RE.search(texto)
        or _TRECHO_OPACO_RE.search(texto)
    ):
        raise MaterialSensivelRecusadoError(
            "Resumo de diagnóstico só aceita texto curto de apresentação."
        )
    return texto


def _normalizar_diagnostico(
    codigo: str | None,
    resumo: str | None,
) -> tuple[str | None, str | None]:
    codigo_norm = None
    if codigo is not None and str(codigo).strip():
        codigo_norm = _validar_diagnostico_codigo(str(codigo))
    resumo_norm = None
    if resumo is not None and str(resumo).strip():
        resumo_norm = _validar_diagnostico_resumo(str(resumo))
    return codigo_norm, resumo_norm


def _normalizar_identificador_externo(valor: str | None) -> str | None:
    if valor is None or not str(valor).strip():
        return None
    texto = str(valor).strip()
    if not _IDENTIFICADOR_EXTERNO_RE.fullmatch(texto):
        raise MaterialSensivelRecusadoError("Identificador externo inválido.")
    _recusar_material_sensivel(texto, "identificador_externo")
    return texto


def _montar_conexao(
    *,
    plugin_id: int,
    user_id: int,
    titularidade: str,
    usuario_originador_id: int,
    conta_id: int | None,
    identificador_externo: str | None,
) -> PluginConexao:
    plugin = _plugin_existente(plugin_id)
    if plugin.status != Plugin.STATUS_DISPONIVEL:
        raise PluginInvalidoError("Plugin fora do catálogo operacional.")
    _usuario_existente(user_id)
    _usuario_existente(usuario_originador_id)
    conta_resolvida = _validar_titularidade(
        plugin, titularidade=titularidade, conta_id=conta_id
    )
    if obter_conexao_usuario(user_id, plugin.id) is not None:
        raise ConexaoAtivaDuplicadaError(
            "Este usuário já possui conexão ativa para este plugin."
        )
    agora = utcnow_naive()
    conexao = PluginConexao(
        plugin_id=plugin.id,
        user_id=int(user_id),
        conta_id=conta_resolvida,
        # Criador da conexão. Ações posteriores gravam o originador no evento.
        usuario_originador_id=int(usuario_originador_id),
        titularidade=titularidade,
        identificador_externo=_normalizar_identificador_externo(identificador_externo),
        estado=PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
        created_at=agora,
        updated_at=agora,
        estado_alterado_em=agora,
    )
    with _unidade_transacional():
        db.session.add(conexao)
        db.session.flush()
        db.session.add(
            PluginCredencialReferencia(
                conexao_id=conexao.id,
                estado=PluginCredencialReferencia.ESTADO_NAO_PROVISIONADA,
                cofre_referencia=None,
                created_at=agora,
                updated_at=agora,
            )
        )
        _registrar_evento(
            tipo_evento=PluginEventoCentral.TIPO_CONEXAO_CRIADA,
            detalhe_codigo=PluginEventoCentral.DETALHE_CRIADA,
            conexao=conexao,
            usuario_originador_id=usuario_originador_id,
            estado_anterior=None,
            estado_novo=conexao.estado,
        )
    return conexao


def criar_conexao(
    *,
    plugin_id: int,
    user_id: int,
    titularidade: str,
    usuario_originador_id: int,
    conta_id: int | None = None,
    identificador_externo: str | None = None,
    commit: bool = True,
) -> PluginConexao:
    """Cria conexão, referência de credencial e evento na mesma unidade.

    usuario_originador_id é o usuário que cria a conexão. O serviço não
    reescreve esse campo depois. Cada ação futura informa o próprio
    originador no evento.
    """
    conexao = _montar_conexao(
        plugin_id=plugin_id,
        user_id=user_id,
        titularidade=titularidade,
        usuario_originador_id=usuario_originador_id,
        conta_id=conta_id,
        identificador_externo=identificador_externo,
    )
    _finalizar(commit)
    return conexao


def _conexao_obrigatoria(conexao_id: int) -> PluginConexao:
    conexao = db.session.get(PluginConexao, int(conexao_id))
    if conexao is None:
        raise ConexaoNaoEncontradaError("Conexão inexistente.")
    return conexao


def alterar_estado_conexao(
    conexao_id: int,
    estado_novo: str,
    *,
    usuario_originador_id: int,
    diagnostico_codigo: str | None = None,
    diagnostico_resumo: str | None = None,
    commit: bool = True,
) -> PluginConexao:
    if estado_novo not in PluginConexao.ESTADOS:
        raise EstadoConexaoInvalidoError("Estado de conexão desconhecido.")
    _usuario_existente(usuario_originador_id)
    codigo, resumo = _normalizar_diagnostico(diagnostico_codigo, diagnostico_resumo)
    conexao = _conexao_obrigatoria(conexao_id)
    if conexao.revogado_em is not None and estado_novo != PluginConexao.ESTADO_DESCONECTADO:
        raise EstadoConexaoInvalidoError(
            "Conexão revogada permanece desconectada."
        )
    if conexao.estado == estado_novo and codigo is None and resumo is None:
        return conexao
    anterior = conexao.estado
    agora = utcnow_naive()

    def _aplicar() -> None:
        conexao.estado = estado_novo
        conexao.estado_alterado_em = agora
        conexao.updated_at = agora
        if estado_novo == PluginConexao.ESTADO_DESCONECTADO:
            conexao.desconectado_em = agora
        elif conexao.revogado_em is None:
            conexao.desconectado_em = None
        if codigo is not None:
            conexao.diagnostico_codigo = codigo
        if resumo is not None:
            conexao.diagnostico_resumo = resumo
        db.session.add(conexao)
        _registrar_evento(
            tipo_evento=_tipo_evento_de_estado(estado_novo),
            detalhe_codigo=PluginEventoCentral.DETALHE_ESTADO_ALTERADO,
            conexao=conexao,
            usuario_originador_id=usuario_originador_id,
            estado_anterior=anterior,
            estado_novo=estado_novo,
        )

    with _unidade_transacional():
        _aplicar()
    _finalizar(commit)
    return conexao


def desconectar_conexao(
    conexao_id: int,
    *,
    usuario_originador_id: int,
    commit: bool = True,
) -> PluginConexao:
    return alterar_estado_conexao(
        conexao_id,
        PluginConexao.ESTADO_DESCONECTADO,
        usuario_originador_id=usuario_originador_id,
        commit=commit,
    )


def substituir_conexao(
    conexao_id: int,
    *,
    usuario_originador_id: int,
    titularidade: str,
    conta_id: int | None = None,
    identificador_externo: str | None = None,
    commit: bool = True,
) -> PluginConexao:
    """
    Encerra a conexão atual e abre outra no mesmo plugin/usuário.

    O UPDATE para desconectado ocorre antes do INSERT, para o índice parcial
    liberar o slot na mesma transação.
    """
    atual = _conexao_obrigatoria(conexao_id)
    if not atual.ocupa_slot():
        raise ConexaoNaoEncontradaError("Não há conexão ativa para substituir.")
    with _unidade_transacional():
        alterar_estado_conexao(
            atual.id,
            PluginConexao.ESTADO_DESCONECTADO,
            usuario_originador_id=usuario_originador_id,
            commit=False,
        )
        nova = _montar_conexao(
            plugin_id=atual.plugin_id,
            user_id=atual.user_id,
            titularidade=titularidade,
            usuario_originador_id=usuario_originador_id,
            conta_id=conta_id,
            identificador_externo=identificador_externo,
        )
    _finalizar(commit)
    return nova


def obter_restricao_usuario(
    conexao_id: int,
    capability_id: int,
) -> PluginRestricaoUsuario | None:
    """None é ausência de restrição explícita, não permissão administrativa."""
    return PluginRestricaoUsuario.query.filter_by(
        conexao_id=int(conexao_id),
        capability_id=int(capability_id),
    ).first()


def obter_concessao_provedor(
    conexao_id: int,
    capability_id: int,
) -> PluginConcessaoProvedor | None:
    """None significa concessão ainda não registrada, não capability concedida."""
    return PluginConcessaoProvedor.query.filter_by(
        conexao_id=int(conexao_id),
        capability_id=int(capability_id),
    ).first()


def registrar_concessao_provedor(
    *,
    conexao_id: int,
    capability_id: int,
    resultado: str,
    commit: bool = True,
) -> PluginConcessaoProvedor:
    """Grava o resultado já conhecido do provider.

    Função interna do domínio, para o adapter futuro e para o teste do
    domínio. Não consulta API, não executa capability e não autoriza uso.
    A autorização continua em avaliar_autorizacao_plugin.
    """
    if resultado not in PluginConcessaoProvedor.RESULTADOS:
        raise ConcessaoProvedorInvalidaError("Resultado de concessão desconhecido.")
    conexao = _conexao_obrigatoria(conexao_id)
    capability = db.session.get(PluginCapability, int(capability_id))
    if capability is None or capability.plugin_id != conexao.plugin_id:
        raise ConcessaoProvedorInvalidaError(
            "Capability não pertence ao plugin desta conexão."
        )
    agora = utcnow_naive()
    existente = obter_concessao_provedor(conexao.id, capability.id)

    def _aplicar() -> PluginConcessaoProvedor:
        atual = existente
        if atual is None:
            atual = PluginConcessaoProvedor(
                conexao_id=conexao.id,
                capability_id=capability.id,
                resultado=resultado,
                created_at=agora,
                updated_at=agora,
            )
            db.session.add(atual)
            return atual
        atual.resultado = resultado
        atual.updated_at = agora
        db.session.add(atual)
        return atual

    with _unidade_transacional():
        concessao = _aplicar()
    _finalizar(commit)
    return concessao


def _capability_liberada_na_conexao(
    conexao: PluginConexao,
    capability_id: int,
) -> PluginCapability:
    capability = db.session.get(PluginCapability, int(capability_id))
    if capability is None or capability.plugin_id != conexao.plugin_id:
        raise RestricaoUsuarioInvalidaError(
            "Capability não pertence ao plugin desta conexão."
        )
    if capability.politica_maxima != PluginCapability.POLITICA_PERMITIDA:
        raise RestricaoUsuarioInvalidaError(
            "Capability não foi liberada pela política máxima da LogCompleta."
        )
    if capability.status != PluginCapability.STATUS_DISPONIVEL:
        raise RestricaoUsuarioInvalidaError("Capability não está disponível.")
    return capability


def registrar_restricao_usuario(
    *,
    conexao_id: int,
    capability_id: int,
    user_id: int,
    usuario_originador_id: int,
    commit: bool = True,
) -> PluginRestricaoUsuario:
    conexao = _conexao_obrigatoria(conexao_id)
    if conexao.user_id != int(user_id):
        raise RestricaoUsuarioInvalidaError(
            "A restrição só pode ser registrada na conexão deste usuário."
        )
    _usuario_existente(usuario_originador_id)
    capability = _capability_liberada_na_conexao(conexao, capability_id)
    agora = utcnow_naive()
    restricao = PluginRestricaoUsuario(
        conexao_id=conexao.id,
        capability_id=capability.id,
        user_id=conexao.user_id,
        efeito=PluginRestricaoUsuario.EFEITO_BLOQUEADA_PELO_USUARIO,
        created_at=agora,
        updated_at=agora,
    )
    with _unidade_transacional():
        db.session.add(restricao)
        _registrar_evento(
            tipo_evento=PluginEventoCentral.TIPO_RESTRICAO_USUARIO_ALTERADA,
            detalhe_codigo=PluginEventoCentral.DETALHE_RESTRICAO_REGISTRADA,
            conexao=conexao,
            usuario_originador_id=usuario_originador_id,
            capability_id=capability.id,
        )
    _finalizar(commit)
    return restricao


def remover_restricao_usuario(
    *,
    conexao_id: int,
    capability_id: int,
    user_id: int,
    usuario_originador_id: int,
    commit: bool = True,
) -> None:
    conexao = _conexao_obrigatoria(conexao_id)
    if conexao.user_id != int(user_id):
        raise RestricaoUsuarioInvalidaError(
            "A restrição só pode ser removida da conexão deste usuário."
        )
    _usuario_existente(usuario_originador_id)
    restricao = obter_restricao_usuario(conexao.id, int(capability_id))
    if restricao is None or restricao.user_id != conexao.user_id:
        raise RestricaoUsuarioInvalidaError("Restrição inexistente para este usuário.")
    capability_id_evento = restricao.capability_id
    with _unidade_transacional():
        db.session.delete(restricao)
        _registrar_evento(
            tipo_evento=PluginEventoCentral.TIPO_RESTRICAO_USUARIO_ALTERADA,
            detalhe_codigo=PluginEventoCentral.DETALHE_RESTRICAO_REMOVIDA,
            conexao=conexao,
            usuario_originador_id=usuario_originador_id,
            capability_id=capability_id_evento,
        )
    _finalizar(commit)


def marcar_credencial_invalida(conexao_id: int, *, commit: bool = True) -> PluginCredencialReferencia:
    """
    Marca a referência como inválida sem desconectar nem apagar a conexão.

    Não recebe segredo. Não grava token.
    """
    conexao = _conexao_obrigatoria(conexao_id)
    referencia = PluginCredencialReferencia.query.filter_by(conexao_id=conexao.id).first()
    if referencia is None:
        raise ConexaoNaoEncontradaError("Referência de credencial inexistente.")
    estado_conexao = conexao.estado
    with _unidade_transacional():
        referencia.estado = PluginCredencialReferencia.ESTADO_INVALIDA
        referencia.updated_at = utcnow_naive()
        db.session.add(referencia)
    _finalizar(commit)
    db.session.refresh(conexao)
    if conexao.estado != estado_conexao:
        raise CentralPluginError("Credencial inválida não pode alterar o estado da conexão.")
    return referencia


def vincular_referencia_cofre(
    conexao_id: int,
    cofre_referencia: str,
    *,
    commit: bool = True,
) -> PluginCredencialReferencia:
    """
    Associa um identificador opaco de cofre. O parâmetro não é o segredo.
    """
    texto = (cofre_referencia or "").strip().lower()
    if not _COFRE_REF_RE.fullmatch(texto) or len(texto) > 80:
        raise MaterialSensivelRecusadoError("Referência de cofre inválida.")
    _recusar_material_sensivel(texto, "cofre_referencia")
    conexao = _conexao_obrigatoria(conexao_id)
    referencia = PluginCredencialReferencia.query.filter_by(conexao_id=conexao.id).first()
    if referencia is None:
        raise ConexaoNaoEncontradaError("Referência de credencial inexistente.")
    with _unidade_transacional():
        referencia.cofre_referencia = texto
        referencia.estado = PluginCredencialReferencia.ESTADO_REFERENCIADA
        referencia.updated_at = utcnow_naive()
        db.session.add(referencia)
    _finalizar(commit)
    return referencia


def representacao_publica_conexao(conexao: PluginConexao) -> dict:
    """Allowlist. Não inclui credencial, cofre nem payload de provider."""

    def _dt(valor):
        return valor.isoformat() if valor is not None else None

    bruta = {
        "id": conexao.id,
        "plugin_id": conexao.plugin_id,
        "user_id": conexao.user_id,
        "conta_id": conexao.conta_id,
        "usuario_originador_id": conexao.usuario_originador_id,
        "titularidade": conexao.titularidade,
        "identificador_externo": conexao.identificador_externo,
        "estado": conexao.estado,
        "diagnostico_codigo": conexao.diagnostico_codigo,
        "diagnostico_resumo": conexao.diagnostico_resumo,
        "created_at": _dt(conexao.created_at),
        "updated_at": _dt(conexao.updated_at),
        "estado_alterado_em": _dt(conexao.estado_alterado_em),
        "desconectado_em": _dt(conexao.desconectado_em),
        "revogado_em": _dt(conexao.revogado_em),
    }
    return {chave: bruta[chave] for chave in _CAMPOS_PUBLICOS_CONEXAO}


# --- Autorização efetiva (lote 2) ---
#
# política máxima da LogCompleta ∩ restrição do usuário ∩ concessão do provider
# Uma camada inferior não reabre o que a superior negou.
#
# Estado operacional de execução: somente PluginConexao.ESTADO_CONECTADO.
# disponivel, aguardando_configuracao, requer_atencao, desabilitado, bloqueado
# e desconectado ocupam ou encerram o slot, mas não autorizam execução.
# O catálogo não trata esses estados como sessão pronta para capability.

_CAMADA_CONTEXTO = "contexto"
_CAMADA_PLUGIN = "plugin"
_CAMADA_CONEXAO = "conexao"
_CAMADA_CAPABILITY = "capability"
_CAMADA_POLITICA = "politica_logcompleta"
_CAMADA_USUARIO = "restricao_usuario"
_CAMADA_PROVEDOR = "concessao_provedor"
_CAMADA_EFETIVA = "efetiva"

_RESTRICAO_AUSENTE = "ausente"
_CONCESSAO_AUSENTE = "ausente"


@dataclass(frozen=True)
class ContextoAutorizacaoPlugin:
    """Identidade e alvo fornecidos pelo chamador.

    Nenhum campo ausente é preenchido com usuário interno, Conta padrão,
    Franquia ou sessão Flask. conta_id None significa que a Conta não faz
    parte deste pedido. correlation_id é opcional e não é persistido.
    """

    user_id: int | None = None
    conta_id: int | None = None
    plugin_id: int | None = None
    plugin_slug: str | None = None
    conexao_id: int | None = None
    capability_id: int | None = None
    capability_chave: str | None = None
    canal_origem: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class PoliticaEfetivaPlugin:
    """Camadas já consultadas. None significa que a camada não foi lida.

    restricao_usuario ausente significa que o usuário não bloqueou.
    concessao_provedor ausente significa que não há registro. Nenhum dos
    dois, sozinho, é autorização.
    """

    politica_maxima: str | None = None
    restricao_usuario: str | None = None
    concessao_provedor: str | None = None


@dataclass(frozen=True)
class ResultadoAutorizacaoPlugin:
    """Decisão fechada. motivo_codigo é a taxonomia segura, sem texto livre."""

    permitido: bool
    motivo_codigo: str
    camada: str
    plugin_id: int | None = None
    plugin_slug: str | None = None
    conexao_id: int | None = None
    capability_id: int | None = None
    capability_chave: str | None = None
    politica_efetiva: PoliticaEfetivaPlugin = PoliticaEfetivaPlugin()
    canal_origem: str | None = None
    correlation_id: str | None = None

    def para_dict(self) -> dict:
        """Allowlist da decisão. Sem token, cofre, diagnóstico ou payload."""
        return {
            "permitido": self.permitido,
            "motivo_codigo": self.motivo_codigo,
            "camada": self.camada,
            "plugin_id": self.plugin_id,
            "plugin_slug": self.plugin_slug,
            "conexao_id": self.conexao_id,
            "capability_id": self.capability_id,
            "capability_chave": self.capability_chave,
            "politica_efetiva": {
                "politica_maxima": self.politica_efetiva.politica_maxima,
                "restricao_usuario": self.politica_efetiva.restricao_usuario,
                "concessao_provedor": self.politica_efetiva.concessao_provedor,
            },
            "canal_origem": self.canal_origem,
            "correlation_id": self.correlation_id,
        }


def _id_explicito(valor) -> tuple[int | None, bool]:
    """(id, invalido). None é ausência. Tipo errado ou não positivo é inválido."""
    if valor is None:
        return None, False
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None, True
    return valor, False


def _normalizar_canal(valor) -> str | None:
    if not isinstance(valor, str):
        return None
    texto = valor.strip().lower()
    if not _CANAL_RE.fullmatch(texto):
        return None
    return texto


def _normalizar_correlation(valor) -> tuple[str | None, bool]:
    """(correlation segura, invalida). Vazio é omissão, não permissão."""
    if valor is None:
        return None, False
    if not isinstance(valor, str):
        return None, True
    texto = valor.strip()
    if not texto:
        return None, False
    if not _CORRELATION_RE.fullmatch(texto):
        return None, True
    if any(marca in texto.casefold() for marca in _MARCAS_SECRETAS):
        return None, True
    return texto, False


def _slug_explicito(valor) -> tuple[str | None, bool]:
    if valor is None:
        return None, False
    if not isinstance(valor, str):
        return None, True
    texto = valor.strip().lower()
    if not texto:
        return None, False
    if not _SLUG_RE.fullmatch(texto) or len(texto) > 80:
        return None, True
    return texto, False


def _chave_explicita(valor) -> tuple[str | None, bool]:
    if valor is None:
        return None, False
    if not isinstance(valor, str):
        return None, True
    texto = valor.strip().lower()
    if not texto:
        return None, False
    if not _CHAVE_RE.fullmatch(texto) or len(texto) > 80:
        return None, True
    return texto, False


def _resultado_autorizacao(
    *,
    permitido: bool,
    motivo_codigo: str,
    camada: str,
    canal_origem: str | None,
    correlation_id: str | None,
    plugin: Plugin | None = None,
    conexao: PluginConexao | None = None,
    capability: PluginCapability | None = None,
    politica_efetiva: PoliticaEfetivaPlugin | None = None,
) -> ResultadoAutorizacaoPlugin:
    return ResultadoAutorizacaoPlugin(
        permitido=permitido,
        motivo_codigo=motivo_codigo,
        camada=camada,
        plugin_id=plugin.id if plugin is not None else None,
        plugin_slug=plugin.slug if plugin is not None else None,
        conexao_id=conexao.id if conexao is not None else None,
        capability_id=capability.id if capability is not None else None,
        capability_chave=capability.chave if capability is not None else None,
        politica_efetiva=politica_efetiva or PoliticaEfetivaPlugin(),
        canal_origem=canal_origem,
        correlation_id=correlation_id,
    )


def _atualizado_depois(entidade, marco) -> bool:
    if entidade is None or marco is None:
        return False
    atualizado = getattr(entidade, "updated_at", None)
    return atualizado is not None and atualizado > marco


def _decisao_reaberta_desde(
    evento: PluginEventoCentral,
    conexao: PluginConexao,
    capability_id: int | None,
) -> bool:
    """True quando um fato que pode autorizar mudou depois desta negação.

    ALLOW não grava evento. Concessão, status do plugin e status ou política
    da capability também não gravam. O updated_at deles é o sinal de que a
    negação seguinte não é repetição consecutiva. Estado da conexão e
    restrição do usuário já inserem outro evento e não dependem deste sinal.
    """
    marco = evento.created_at
    if _atualizado_depois(db.session.get(Plugin, conexao.plugin_id), marco):
        return True
    if capability_id is None:
        return False
    if _atualizado_depois(db.session.get(PluginCapability, capability_id), marco):
        return True
    return _atualizado_depois(
        obter_concessao_provedor(conexao.id, capability_id),
        marco,
    )


def _negacao_ja_registrada(
    conexao: PluginConexao,
    *,
    motivo_codigo: str,
    capability_id: int | None,
    usuario_originador_id: int,
) -> bool:
    """Repetição consecutiva da mesma negação.

    O último evento igual não basta: ALLOW não é evento. Se a concessão, o
    plugin ou a capability mudou depois dele, a negação é outra decisão.
    """
    ultima = (
        PluginEventoCentral.query.filter_by(conexao_id=conexao.id)
        .order_by(PluginEventoCentral.id.desc())
        .first()
    )
    if ultima is None:
        return False
    identica = (
        ultima.tipo_evento == PluginEventoCentral.TIPO_AUTORIZACAO_NEGADA
        and ultima.detalhe_codigo == motivo_codigo
        and ultima.capability_id == capability_id
        and ultima.user_id == conexao.user_id
        and ultima.usuario_originador_id == int(usuario_originador_id)
    )
    if not identica:
        return False
    return not _decisao_reaberta_desde(ultima, conexao, capability_id)


def _auditar_negacao(
    *,
    conexao: PluginConexao,
    motivo_codigo: str,
    usuario_originador_id: int,
    capability_id: int | None,
    commit: bool,
) -> None:
    """Um evento por negação consecutiva idêntica. Repetição imediata não reinsere.

    DENY depois de ALLOW reinsere: a permissão não grava evento, mas a mudança
    da concessão, do plugin ou da capability fica no updated_at.

    Só ocorre com conexão carregada: a trilha exige conexao_id. Não grava
    correlation, diagnóstico, cofre nem payload.
    """
    if motivo_codigo not in PluginEventoCentral.MOTIVOS_NEGACAO:
        return
    # commit True só alcança a sessão se esta chamada inseriu o evento.
    # Negação idêntica consecutiva não reinsere e não confirma pendência alheia.
    inseriu = False
    with _unidade_transacional():
        if _negacao_ja_registrada(
            conexao,
            motivo_codigo=motivo_codigo,
            capability_id=capability_id,
            usuario_originador_id=usuario_originador_id,
        ):
            return
        _registrar_evento(
            tipo_evento=PluginEventoCentral.TIPO_AUTORIZACAO_NEGADA,
            detalhe_codigo=motivo_codigo,
            conexao=conexao,
            usuario_originador_id=usuario_originador_id,
            capability_id=capability_id,
        )
        inseriu = True
    if inseriu:
        _finalizar(commit)


def _registrar_negacao_se_houver_conexao(
    resultado: ResultadoAutorizacaoPlugin,
    *,
    conexao: PluginConexao | None,
    usuario_originador_id: int | None,
    commit: bool,
) -> ResultadoAutorizacaoPlugin:
    if resultado.permitido or conexao is None or usuario_originador_id is None:
        return resultado
    _auditar_negacao(
        conexao=conexao,
        motivo_codigo=resultado.motivo_codigo,
        usuario_originador_id=usuario_originador_id,
        capability_id=resultado.capability_id,
        commit=commit,
    )
    logger.info(
        "autorizacao_plugin permitido=%s motivo=%s camada=%s user_id=%s plugin_id=%s conexao_id=%s capability_id=%s",
        resultado.permitido,
        resultado.motivo_codigo,
        resultado.camada,
        usuario_originador_id,
        resultado.plugin_id,
        resultado.conexao_id,
        resultado.capability_id,
    )
    return resultado


def _resolver_plugin(
    plugin_id: int | None,
    plugin_slug: str | None,
) -> tuple[Plugin | None, str | None]:
    por_id = db.session.get(Plugin, plugin_id) if plugin_id is not None else None
    por_slug = (
        Plugin.query.filter_by(slug=plugin_slug).first() if plugin_slug is not None else None
    )
    if plugin_id is not None and plugin_slug is not None:
        if por_id is None or por_slug is None:
            return None, PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE
        if por_id.id != por_slug.id:
            return None, PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
        return por_id, None
    if plugin_id is not None:
        if por_id is None:
            return None, PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE
        return por_id, None
    if por_slug is None:
        return None, PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE
    return por_slug, None


def _resolver_capability(
    plugin: Plugin,
    *,
    capability_id: int | None,
    capability_chave: str | None,
) -> tuple[PluginCapability | None, str | None, str | None]:
    """(capability, motivo, camada). Ids de outro plugin contam como inexistentes."""
    por_id = None
    if capability_id is not None:
        por_id = db.session.get(PluginCapability, capability_id)
        if por_id is None or por_id.plugin_id != plugin.id:
            return None, PluginEventoCentral.MOTIVO_CAPABILITY_INEXISTENTE, _CAMADA_CAPABILITY
    por_chave = None
    if capability_chave is not None:
        por_chave = PluginCapability.query.filter_by(
            plugin_id=plugin.id,
            chave=capability_chave,
        ).first()
        if por_chave is None:
            return None, PluginEventoCentral.MOTIVO_CAPABILITY_INEXISTENTE, _CAMADA_CAPABILITY
    if por_id is not None and por_chave is not None and por_id.id != por_chave.id:
        return None, PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO, _CAMADA_CONTEXTO
    return por_id or por_chave, None, None


def avaliar_autorizacao_plugin(
    contexto: ContextoAutorizacaoPlugin,
    *,
    commit: bool = False,
) -> ResultadoAutorizacaoPlugin:
    """Decide se a capability pedida pode ser executada.

    Ordem: contexto, plugin, conexão, capability, política LogCompleta,
    restrição do usuário, concessão do provider. A primeira negação encerra.
    commit False não confirma pendências do chamador. commit True só confirma
    a sessão quando um evento novo de negação foi inserido.
    Não chama adapter nem API.
    """
    if not isinstance(contexto, ContextoAutorizacaoPlugin):
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
            camada=_CAMADA_CONTEXTO,
            canal_origem=None,
            correlation_id=None,
        )

    user_id, user_invalido = _id_explicito(contexto.user_id)
    conta_id, conta_invalida = _id_explicito(contexto.conta_id)
    plugin_id, plugin_id_invalido = _id_explicito(contexto.plugin_id)
    conexao_id, conexao_id_invalida = _id_explicito(contexto.conexao_id)
    capability_id, capability_id_invalida = _id_explicito(contexto.capability_id)
    plugin_slug, slug_invalido = _slug_explicito(contexto.plugin_slug)
    capability_chave, chave_invalida = _chave_explicita(contexto.capability_chave)
    canal = _normalizar_canal(contexto.canal_origem)
    correlation, correlation_invalida = _normalizar_correlation(contexto.correlation_id)
    contexto_incompleto = (
        user_invalido
        or conta_invalida
        or plugin_id_invalido
        or conexao_id_invalida
        or capability_id_invalida
        or slug_invalido
        or chave_invalida
        or correlation_invalida
        or user_id is None
        or conexao_id is None
        or canal is None
        or (plugin_id is None and plugin_slug is None)
        or (capability_id is None and capability_chave is None)
    )
    if contexto_incompleto:
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
            camada=_CAMADA_CONTEXTO,
            canal_origem=canal,
            correlation_id=None,
        )

    if db.session.get(User, user_id) is None:
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
            camada=_CAMADA_CONTEXTO,
            canal_origem=canal,
            correlation_id=correlation,
        )

    plugin, motivo_plugin = _resolver_plugin(plugin_id, plugin_slug)
    if motivo_plugin == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO or plugin is None:
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=motivo_plugin or PluginEventoCentral.MOTIVO_PLUGIN_INEXISTENTE,
            camada=(
                _CAMADA_CONTEXTO
                if motivo_plugin == PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO
                else _CAMADA_PLUGIN
            ),
            canal_origem=canal,
            correlation_id=correlation,
            plugin=plugin,
        )
    if plugin.status != Plugin.STATUS_DISPONIVEL:
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=PluginEventoCentral.MOTIVO_PLUGIN_INDISPONIVEL,
            camada=_CAMADA_PLUGIN,
            canal_origem=canal,
            correlation_id=correlation,
            plugin=plugin,
        )

    conexao = db.session.get(PluginConexao, conexao_id)
    if conexao is None:
        return _resultado_autorizacao(
            permitido=False,
            motivo_codigo=PluginEventoCentral.MOTIVO_CONEXAO_INEXISTENTE,
            camada=_CAMADA_CONEXAO,
            canal_origem=canal,
            correlation_id=correlation,
            plugin=plugin,
        )
    if conexao.user_id != user_id:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONEXAO_NAO_PERTENCE,
                camada=_CAMADA_CONEXAO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if conta_id is not None and conexao.conta_id != conta_id:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONTA_INCOMPATIVEL,
                camada=_CAMADA_CONEXAO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if conexao.plugin_id != plugin.id:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
                camada=_CAMADA_CONTEXTO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if conexao.estado != PluginConexao.ESTADO_CONECTADO:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONEXAO_NAO_OPERACIONAL,
                camada=_CAMADA_CONEXAO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )

    capability, motivo_capability, camada_capability = _resolver_capability(
        plugin,
        capability_id=capability_id,
        capability_chave=capability_chave,
    )
    if capability is None:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=motivo_capability or PluginEventoCentral.MOTIVO_CAPABILITY_INEXISTENTE,
                camada=camada_capability or _CAMADA_CAPABILITY,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if capability.status != PluginCapability.STATUS_DISPONIVEL:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CAPABILITY_INDISPONIVEL,
                camada=_CAMADA_CAPABILITY,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if capability.politica_maxima != PluginCapability.POLITICA_PERMITIDA:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_BLOQUEADA_PELA_PLATAFORMA,
                camada=_CAMADA_POLITICA,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
                politica_efetiva=PoliticaEfetivaPlugin(
                    politica_maxima=capability.politica_maxima,
                ),
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )

    politica_plataforma = PoliticaEfetivaPlugin(
        politica_maxima=PluginCapability.POLITICA_PERMITIDA,
    )
    restricao = obter_restricao_usuario(conexao.id, capability.id)
    if restricao is not None:
        capability_restricao = db.session.get(PluginCapability, restricao.capability_id)
        inconsistente = (
            restricao.conexao_id != conexao.id
            or restricao.user_id != conexao.user_id
            or restricao.capability_id != capability.id
            or capability_restricao is None
            or capability_restricao.plugin_id != conexao.plugin_id
            or restricao.efeito != PluginRestricaoUsuario.EFEITO_BLOQUEADA_PELO_USUARIO
        )
        if inconsistente:
            return _registrar_negacao_se_houver_conexao(
                _resultado_autorizacao(
                    permitido=False,
                    motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
                    camada=_CAMADA_CONTEXTO,
                    canal_origem=canal,
                    correlation_id=correlation,
                    plugin=plugin,
                    conexao=conexao,
                    capability=capability,
                    politica_efetiva=politica_plataforma,
                ),
                conexao=conexao,
                usuario_originador_id=user_id,
                commit=commit,
            )
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_BLOQUEADA_PELO_USUARIO,
                camada=_CAMADA_USUARIO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
                politica_efetiva=PoliticaEfetivaPlugin(
                    politica_maxima=PluginCapability.POLITICA_PERMITIDA,
                    restricao_usuario=restricao.efeito,
                ),
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )

    concessao = obter_concessao_provedor(conexao.id, capability.id)
    if concessao is None:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_AUSENTE,
                camada=_CAMADA_PROVEDOR,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
                politica_efetiva=PoliticaEfetivaPlugin(
                    politica_maxima=PluginCapability.POLITICA_PERMITIDA,
                    restricao_usuario=_RESTRICAO_AUSENTE,
                    concessao_provedor=_CONCESSAO_AUSENTE,
                ),
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    capability_concessao = db.session.get(PluginCapability, concessao.capability_id)
    concessao_inconsistente = (
        concessao.conexao_id != conexao.id
        or concessao.capability_id != capability.id
        or capability_concessao is None
        or capability_concessao.plugin_id != conexao.plugin_id
        or concessao.resultado not in PluginConcessaoProvedor.RESULTADOS
    )
    if concessao_inconsistente:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONTEXTO_INVALIDO,
                camada=_CAMADA_CONTEXTO,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
                politica_efetiva=PoliticaEfetivaPlugin(
                    politica_maxima=PluginCapability.POLITICA_PERMITIDA,
                    restricao_usuario=_RESTRICAO_AUSENTE,
                ),
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )
    if concessao.resultado != PluginConcessaoProvedor.RESULTADO_CONCEDIDA:
        return _registrar_negacao_se_houver_conexao(
            _resultado_autorizacao(
                permitido=False,
                motivo_codigo=PluginEventoCentral.MOTIVO_CONCESSAO_PROVEDOR_NEGADA,
                camada=_CAMADA_PROVEDOR,
                canal_origem=canal,
                correlation_id=correlation,
                plugin=plugin,
                conexao=conexao,
                capability=capability,
                politica_efetiva=PoliticaEfetivaPlugin(
                    politica_maxima=PluginCapability.POLITICA_PERMITIDA,
                    restricao_usuario=_RESTRICAO_AUSENTE,
                    concessao_provedor=concessao.resultado,
                ),
            ),
            conexao=conexao,
            usuario_originador_id=user_id,
            commit=commit,
        )

    return _resultado_autorizacao(
        permitido=True,
        motivo_codigo=PluginEventoCentral.MOTIVO_AUTORIZADA,
        camada=_CAMADA_EFETIVA,
        canal_origem=canal,
        correlation_id=correlation,
        plugin=plugin,
        conexao=conexao,
        capability=capability,
        politica_efetiva=PoliticaEfetivaPlugin(
            politica_maxima=PluginCapability.POLITICA_PERMITIDA,
            restricao_usuario=_RESTRICAO_AUSENTE,
            concessao_provedor=PluginConcessaoProvedor.RESULTADO_CONCEDIDA,
        ),
    )
