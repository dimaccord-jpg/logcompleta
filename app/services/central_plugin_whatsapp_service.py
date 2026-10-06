"""Conexão do plugin WhatsApp já cadastrado na Central.

Não cria outro plugin. O fluxo usa o adapter_key da linha existente.
A identidade operacional continua em IdentidadeCanalExterna. Este módulo
não chama a Graph API, não grava telefone e não lê destino vindo do navegador.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from sqlalchemy import text

from app.extensions import db
from app.models import (
    IdentidadeCanalExterna,
    OnboardingCanal,
    Plugin,
    PluginConexao,
    User,
    utcnow_naive,
)
from app.services.canal_aquisicao_config import numero_publico_whatsapp
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.central_plugin_service import (
    ConexaoAtivaDuplicadaError,
    alterar_estado_conexao,
    criar_conexao,
    desconectar_conexao,
    obter_conexao_usuario,
)
from app.services.central_plugin_usuario_service import (
    AcessoConexaoUsuarioNegado,
    AcaoPluginUsuarioRecusada,
    CentralPluginUsuarioError,
    FluxoConexaoPlugin,
    registrar_fluxo_conexao,
    registrar_preparador_fluxo,
    registrar_reconciliador_catalogo,
)

logger = logging.getLogger(__name__)

MENSAGEM_CANAL_INDISPONIVEL = (
    "O WhatsApp ainda não está disponível para conexão neste ambiente. "
    "Nenhuma conexão foi iniciada."
)
MENSAGEM_JA_CONECTADO = "Sua conexão com o WhatsApp já está ativa."
MENSAGEM_REVALIDA_ATIVA = "Sua conexão com o WhatsApp foi confirmada."
MENSAGEM_REVALIDA_AUSENTE = (
    "Não há vínculo ativo do WhatsApp. Abra a conversa para configurar de novo. "
    "A conexão não foi marcada como conectada."
)
MENSAGEM_REVALIDA_INCONSISTENTE = (
    "Há mais de um vínculo ativo do WhatsApp. "
    "A conexão não foi marcada como conectada."
)
_PREFILL = "Quero me cadastrar"
_PREFIXO_DEEP_LINK = "https://wa.me/"
_DIAGNOSTICO_VINCULO = "vinculo_canal"
_RESUMO_VINCULO = "Vinculo do canal confirmado."
_DIAGNOSTICO_AUSENTE = "vinculo_ausente"
_RESUMO_AUSENTE = "Nao ha vinculo ativo."
_DIAGNOSTICO_INCONSISTENTE = "vinculo_inconsistente"
_RESUMO_INCONSISTENTE = "Mais de um vinculo ativo."
_MARCAS_DE_OUTRO_PLUGIN = ("brobot", "agenda", "google", "xc")


class FluxoConexaoWhatsApp(FluxoConexaoPlugin):
    """Abre a conversa oficial e revalida pelo vínculo real. Não é OAuth."""

    reutiliza_aguardando = True
    revalidacao_pode_alterar_estado = True
    revalidar_quando_conectado = True
    desconecta_vinculo_real = True

    def rotulo_conectar(self) -> str:
        return "Conectar WhatsApp"

    def texto_configuracao_ausente(self) -> str | None:
        if numero_publico_whatsapp() is None:
            return MENSAGEM_CANAL_INDISPONIVEL
        return None

    def url_abertura(self) -> str | None:
        return url_deep_link_whatsapp()

    def iniciar_configuracao(self, *, plugin_id: int, user_id: int) -> PluginConexao:
        if numero_publico_whatsapp() is None:
            raise CentralPluginUsuarioError(MENSAGEM_CANAL_INDISPONIVEL)
        usuario_id = int(user_id)
        if identidade_vinculada_do_usuario(usuario_id) is not None:
            raise AcaoPluginUsuarioRecusada(MENSAGEM_JA_CONECTADO)
        plugin = db.session.get(Plugin, int(plugin_id))
        if plugin is None or not plugin.suporta_titularidade_pessoal:
            raise AcaoPluginUsuarioRecusada(
                "A conexão individual desta integração ainda não está disponível."
            )
        existente = obter_conexao_usuario(usuario_id, plugin.id)
        if existente is not None:
            if (
                int(existente.user_id) == usuario_id
                and existente.titularidade == PluginConexao.TITULARIDADE_PESSOAL
                and existente.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
            ):
                return existente
            raise AcaoPluginUsuarioRecusada(
                "Você já tem uma conexão em andamento nesta integração."
            )
        try:
            return criar_conexao(
                plugin_id=plugin.id,
                user_id=usuario_id,
                titularidade=PluginConexao.TITULARIDADE_PESSOAL,
                usuario_originador_id=usuario_id,
            )
        except ConexaoAtivaDuplicadaError:
            de_novo = obter_conexao_usuario(usuario_id, plugin.id)
            if (
                de_novo is not None
                and de_novo.estado == PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO
            ):
                return de_novo
            raise

    def revalidar(self, *, conexao_id: int, user_id: int) -> str:
        return revalidar_whatsapp(conexao_id=int(conexao_id), user_id=int(user_id))

    def desconectar(self, *, user_id: int, plugin_id: int) -> PluginConexao:
        return desconectar_whatsapp_do_usuario(user_id=int(user_id), plugin_id=int(plugin_id))


def url_deep_link_whatsapp() -> str | None:
    """Deep link oficial. O número sai só da configuração do servidor."""
    numero = numero_publico_whatsapp()
    if numero is None:
        return None
    return f"{_PREFIXO_DEEP_LINK}{numero}?text={quote(_PREFILL)}"


def localizar_plugin_whatsapp() -> Plugin | None:
    """A linha já cadastrada. Não insere plugin nem escolhe um candidato ambíguo."""
    candidatos = [plugin for plugin in Plugin.query.order_by(Plugin.id.asc()).all() if _eh_plugin_whatsapp(plugin)]
    if not candidatos:
        return None
    for chave in ("whatsapp_meta",):
        por_chave = [plugin for plugin in candidatos if plugin.adapter_key == chave]
        if len(por_chave) == 1:
            return por_chave[0]
    por_slug = [plugin for plugin in candidatos if plugin.slug == "whatsapp"]
    if len(por_slug) == 1:
        return por_slug[0]
    if len(candidatos) == 1:
        return candidatos[0]
    logger.info("central_plugin_whatsapp plugin_ambiguo quantidade=%s", len(candidatos))
    return None


def garantir_fluxo_whatsapp() -> None:
    plugin = localizar_plugin_whatsapp()
    if plugin is None or not (plugin.adapter_key or "").strip():
        return
    registrar_fluxo_conexao(FluxoConexaoWhatsApp(plugin.adapter_key))


def _identidades_vinculadas(user_id: int) -> list[IdentidadeCanalExterna]:
    return (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            user_id=int(user_id),
            estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        )
        .filter(
            IdentidadeCanalExterna.vinculada_em.isnot(None),
            IdentidadeCanalExterna.revogada_em.is_(None),
        )
        .order_by(IdentidadeCanalExterna.id.asc())
        .all()
    )


def identidade_vinculada_do_usuario(user_id: int) -> IdentidadeCanalExterna | None:
    """Uma identidade WhatsApp coerente deste User. Mais de uma é inconsistência."""
    linhas = _identidades_vinculadas(user_id)
    if len(linhas) != 1:
        return None
    return linhas[0]


def travar_usuario_whatsapp(user_id: int) -> User | None:
    """Serializa vínculo, desconexão e reconciliação deste User.

    PostgreSQL: SELECT ... FOR UPDATE na linha persistida do User.
    SQLite ignora FOR UPDATE; o UPDATE da mesma linha adquire o lock de escrita
    antes da leitura das identidades. Não é lock em memória.
    """
    usuario_id = int(user_id)
    bind = db.session.get_bind()
    dialecto = getattr(getattr(bind, "dialect", None), "name", None)
    if dialecto == "sqlite":
        tabela = User.__table__.name
        db.session.execute(
            text(f'UPDATE "{tabela}" SET id = id WHERE id = :id'),
            {"id": usuario_id},
        )
        return db.session.get(User, usuario_id)
    return (
        db.session.query(User)
        .filter(User.id == usuario_id)
        .with_for_update()
        .one_or_none()
    )


def usuario_ja_tem_outro_whatsapp(user_id: int, identidade_id: int) -> bool:
    outra = (
        IdentidadeCanalExterna.query.filter_by(
            provedor=PROVEDOR_WHATSAPP_META,
            user_id=int(user_id),
            estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        )
        .filter(
            IdentidadeCanalExterna.id != int(identidade_id),
            IdentidadeCanalExterna.vinculada_em.isnot(None),
            IdentidadeCanalExterna.revogada_em.is_(None),
        )
        .first()
    )
    return outra is not None


def sincronizar_conexao_apos_vinculo(identidade: IdentidadeCanalExterna) -> None:
    """Reflete o vínculo já confirmado. Não vincula por e-mail e não faz commit.

    O chamador já segura a trava do User. Conectado exige exatamente uma identidade.
    """
    if not _identidade_acabou_de_vincular(identidade):
        return
    plugin = localizar_plugin_whatsapp()
    if plugin is None or plugin.status != Plugin.STATUS_DISPONIVEL:
        return
    if not plugin.suporta_titularidade_pessoal:
        return
    if len(_identidades_vinculadas(int(identidade.user_id))) != 1:
        _marcar_inconsistencia(int(identidade.user_id), plugin)
        return
    _garantir_conectado(
        int(identidade.user_id),
        plugin,
        registrar_se_ja_conectado=True,
        commit=False,
    )
    logger.info(
        "central_plugin_whatsapp vinculo_refletido user_id=%s identidade_id=%s",
        int(identidade.user_id),
        int(identidade.id),
    )


def reconciliar_exibicao_whatsapp(user_id: int) -> None:
    """O card segue a identidade real. Conectado exige exatamente uma válida."""
    garantir_fluxo_whatsapp()
    plugin = localizar_plugin_whatsapp()
    if plugin is None or plugin.status != Plugin.STATUS_DISPONIVEL:
        return
    if not plugin.suporta_titularidade_pessoal:
        return
    from app.services.central_plugin_service import _unidade_transacional

    usuario_id = int(user_id)
    alterou = False
    with _unidade_transacional():
        if travar_usuario_whatsapp(usuario_id) is None:
            return
        alterou = _alinhar_conexao_ao_vinculo(usuario_id, plugin, registrar_evento_repetido=False)
    if alterou:
        db.session.commit()


def revalidar_whatsapp(*, conexao_id: int, user_id: int) -> str:
    from app.services.central_plugin_service import _unidade_transacional

    usuario_id = int(user_id)
    with _unidade_transacional():
        if travar_usuario_whatsapp(usuario_id) is None:
            raise AcessoConexaoUsuarioNegado(
                "Você só pode administrar a sua própria conexão."
            )
        conexao = _conexao_do_usuario(int(conexao_id), usuario_id)
        classe = _classe_do_vinculo(usuario_id)
        if classe == "conectado":
            if conexao.estado != PluginConexao.ESTADO_CONECTADO:
                alterar_estado_conexao(
                    conexao.id,
                    PluginConexao.ESTADO_CONECTADO,
                    usuario_originador_id=usuario_id,
                    diagnostico_codigo=_DIAGNOSTICO_VINCULO,
                    diagnostico_resumo=_RESUMO_VINCULO,
                    commit=False,
                )
            mensagem = MENSAGEM_REVALIDA_ATIVA
        elif classe == "inconsistente":
            if conexao.estado not in (
                PluginConexao.ESTADO_BLOQUEADO,
                PluginConexao.ESTADO_DESABILITADO,
                PluginConexao.ESTADO_REQUER_ATENCAO,
            ):
                alterar_estado_conexao(
                    conexao.id,
                    PluginConexao.ESTADO_REQUER_ATENCAO,
                    usuario_originador_id=usuario_id,
                    diagnostico_codigo=_DIAGNOSTICO_INCONSISTENTE,
                    diagnostico_resumo=_RESUMO_INCONSISTENTE,
                    commit=False,
                )
            mensagem = MENSAGEM_REVALIDA_INCONSISTENTE
        else:
            if conexao.estado not in (
                PluginConexao.ESTADO_BLOQUEADO,
                PluginConexao.ESTADO_DESABILITADO,
                PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
            ):
                alterar_estado_conexao(
                    conexao.id,
                    PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
                    usuario_originador_id=usuario_id,
                    diagnostico_codigo=_DIAGNOSTICO_AUSENTE,
                    diagnostico_resumo=_RESUMO_AUSENTE,
                    commit=False,
                )
            mensagem = MENSAGEM_REVALIDA_AUSENTE
    db.session.commit()
    return mensagem


def desconectar_whatsapp_do_usuario(*, user_id: int, plugin_id: int) -> PluginConexao:
    """Revoga o vínculo real e a conexão da Central na mesma transação."""
    from app.services.central_plugin_service import _unidade_transacional
    from app.services.onboarding_canal_conclusao_service import (
        revogar_conclusoes_relacionadas_ao_usuario,
    )

    usuario_id = int(user_id)
    with _unidade_transacional():
        if travar_usuario_whatsapp(usuario_id) is None:
            raise AcessoConexaoUsuarioNegado(
                "Você só pode administrar a sua própria conexão."
            )
        agora = utcnow_naive()
        for identidade in _identidades_vinculadas(usuario_id):
            identidade.estado = IdentidadeCanalExterna.ESTADO_REVOGADA
            identidade.revogada_em = agora
            identidade.atualizada_em = agora
            _cancelar_jornadas_abertas(identidade, agora)
        conexao = obter_conexao_usuario(usuario_id, int(plugin_id))
        if conexao is None or int(conexao.user_id) != usuario_id:
            raise AcessoConexaoUsuarioNegado(
                "Você só pode administrar a sua própria conexão."
            )
        if conexao.titularidade != PluginConexao.TITULARIDADE_PESSOAL:
            raise AcaoPluginUsuarioRecusada(
                "Você só pode administrar a sua própria conexão."
            )
        desconectar_conexao(
            conexao.id,
            usuario_originador_id=usuario_id,
            commit=False,
        )
        user = db.session.get(User, usuario_id)
        if user is not None:
            revogar_conclusoes_relacionadas_ao_usuario(user)
    db.session.commit()
    logger.info(
        "central_plugin_whatsapp desconectado user_id=%s conexao_id=%s",
        usuario_id,
        int(conexao.id),
    )
    return conexao


def _eh_plugin_whatsapp(plugin: Plugin) -> bool:
    texto = " ".join(
        (
            plugin.slug or "",
            plugin.nome or "",
            plugin.adapter_key or "",
        )
    ).casefold()
    if any(marca in texto for marca in _MARCAS_DE_OUTRO_PLUGIN):
        return False
    return "whatsapp" in texto


def _identidade_acabou_de_vincular(identidade: IdentidadeCanalExterna) -> bool:
    return (
        identidade.provedor == PROVEDOR_WHATSAPP_META
        and identidade.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
        and identidade.user_id is not None
        and identidade.vinculada_em is not None
        and identidade.revogada_em is None
    )


def _classe_do_vinculo(user_id: int) -> str:
    quantidade = len(_identidades_vinculadas(int(user_id)))
    if quantidade == 1:
        return "conectado"
    if quantidade == 0:
        return "ausente"
    return "inconsistente"


def _slot_pessoal_ajustavel(slot: PluginConexao | None) -> PluginConexao | None:
    if slot is None or slot.titularidade != PluginConexao.TITULARIDADE_PESSOAL:
        return None
    if slot.estado in (
        PluginConexao.ESTADO_BLOQUEADO,
        PluginConexao.ESTADO_DESABILITADO,
    ):
        return None
    return slot


def _afastar_conectado_sem_identidade(user_id: int, plugin: Plugin) -> bool:
    slot = _slot_pessoal_ajustavel(obter_conexao_usuario(int(user_id), plugin.id))
    if slot is None or slot.estado not in (
        PluginConexao.ESTADO_CONECTADO,
        PluginConexao.ESTADO_REQUER_ATENCAO,
    ):
        return False
    alterar_estado_conexao(
        slot.id,
        PluginConexao.ESTADO_AGUARDANDO_CONFIGURACAO,
        usuario_originador_id=int(user_id),
        diagnostico_codigo=_DIAGNOSTICO_AUSENTE,
        diagnostico_resumo=_RESUMO_AUSENTE,
        commit=False,
    )
    return True


def _marcar_inconsistencia(user_id: int, plugin: Plugin) -> bool:
    slot = _slot_pessoal_ajustavel(obter_conexao_usuario(int(user_id), plugin.id))
    if slot is None or slot.estado == PluginConexao.ESTADO_REQUER_ATENCAO:
        return False
    alterar_estado_conexao(
        slot.id,
        PluginConexao.ESTADO_REQUER_ATENCAO,
        usuario_originador_id=int(user_id),
        diagnostico_codigo=_DIAGNOSTICO_INCONSISTENTE,
        diagnostico_resumo=_RESUMO_INCONSISTENTE,
        commit=False,
    )
    return True


def _alinhar_conexao_ao_vinculo(
    user_id: int,
    plugin: Plugin,
    *,
    registrar_evento_repetido: bool,
) -> bool:
    classe = _classe_do_vinculo(int(user_id))
    if classe == "conectado":
        antes = obter_conexao_usuario(int(user_id), plugin.id)
        estado_antes = None if antes is None else antes.estado
        _garantir_conectado(
            int(user_id),
            plugin,
            registrar_se_ja_conectado=registrar_evento_repetido,
            commit=False,
        )
        depois = obter_conexao_usuario(int(user_id), plugin.id)
        if depois is None:
            return False
        return antes is None or estado_antes != depois.estado
    if classe == "ausente":
        return _afastar_conectado_sem_identidade(int(user_id), plugin)
    return _marcar_inconsistencia(int(user_id), plugin)


def _garantir_conectado(
    user_id: int,
    plugin: Plugin,
    *,
    registrar_se_ja_conectado: bool,
    commit: bool,
) -> PluginConexao | None:
    slot = obter_conexao_usuario(user_id, plugin.id)
    if slot is not None and slot.estado in (
        PluginConexao.ESTADO_BLOQUEADO,
        PluginConexao.ESTADO_DESABILITADO,
    ):
        return slot
    if slot is not None and slot.titularidade != PluginConexao.TITULARIDADE_PESSOAL:
        return None
    if slot is None:
        try:
            slot = criar_conexao(
                plugin_id=plugin.id,
                user_id=user_id,
                titularidade=PluginConexao.TITULARIDADE_PESSOAL,
                usuario_originador_id=user_id,
                commit=False,
            )
        except ConexaoAtivaDuplicadaError:
            slot = obter_conexao_usuario(user_id, plugin.id)
            if slot is None:
                raise
    if slot.estado == PluginConexao.ESTADO_CONECTADO and not registrar_se_ja_conectado:
        return slot
    if slot.estado != PluginConexao.ESTADO_CONECTADO or registrar_se_ja_conectado:
        slot = alterar_estado_conexao(
            slot.id,
            PluginConexao.ESTADO_CONECTADO,
            usuario_originador_id=user_id,
            diagnostico_codigo=_DIAGNOSTICO_VINCULO,
            diagnostico_resumo=_RESUMO_VINCULO,
            commit=False,
        )
    if commit:
        db.session.commit()
    return slot


def _conexao_do_usuario(conexao_id: int, user_id: int) -> PluginConexao:
    conexao = db.session.get(PluginConexao, int(conexao_id))
    if conexao is None or int(conexao.user_id) != int(user_id):
        raise AcessoConexaoUsuarioNegado(
            "Você só pode administrar a sua própria conexão."
        )
    return conexao


def _cancelar_jornadas_abertas(identidade: IdentidadeCanalExterna, agora) -> None:
    abertas = (
        OnboardingCanal.query.filter_by(identidade_id=identidade.id)
        .filter(OnboardingCanal.etapa.in_(OnboardingCanal.ETAPAS_ABERTAS))
        .all()
    )
    for jornada in abertas:
        jornada.etapa = OnboardingCanal.ETAPA_CANCELADO
        jornada.cancelada_em = agora
        jornada.atualizada_em = agora


registrar_preparador_fluxo(garantir_fluxo_whatsapp)
registrar_reconciliador_catalogo(reconciliar_exibicao_whatsapp)
