"""Páginas web da conclusão de onboarding por canal. Sem envio e sem Meta."""
from __future__ import annotations

from urllib.parse import urlsplit

from flask import current_app, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user

from app.extensions import db
from app.models import OnboardingCanalConclusao, User
from app.services.canal_aquisicao_config import url_retorno_canal_permitida
from app.services.senha_cadastro import validar_senha_cadastro
from app.services.onboarding_canal_conclusao_service import (
    CODIGO_AUTH_NECESSARIA,
    CODIGO_CONTA_CRIADA,
    CODIGO_CONTA_EXISTENTE,
    CODIGO_JA_REALIZADA,
    CODIGO_LINK_EMITIDO,
    CODIGO_SENHA_DIVERGENTE,
    CODIGO_SENHA_INVALIDA,
    CODIGO_VINCULO_CONFIRMADO,
    CODIGO_VINCULO_DIVERGENTE,
    concluir_definicao_senha,
    concluir_definicao_senha_por_alias,
    confirmar_vinculo_conta_existente,
    confirmar_vinculo_conta_por_alias,
    confirmar_vinculo_handoff,
    inspecionar_alias_conclusao,
    inspecionar_handoff_conclusao,
    inspecionar_link_conclusao,
)

SESSION_HANDOFF_ID = "onboarding_canal_handoff_id"
CAMINHO_CONTINUAR = "/onboarding/canal/continuar"
_PREFIXO_LINK_CONCLUSAO = "/onboarding/canal/concluir/"

_MENSAGENS = {
    "token_invalido": "Este link é inválido.",
    "token_expirado": "Este link expirou.",
    "token_revogado": "Este link não pode mais ser usado.",
    "link_ja_utilizado": "Este link já foi utilizado.",
    "conclusao_ja_realizada": "Este cadastro já foi concluído.",
    "jornada_expirada": "Esta jornada expirou.",
    "jornada_cancelada": "Esta jornada foi cancelada.",
    "jornada_nao_operavel": "Este link não pode concluir o cadastro.",
    "jornada_ausente": "Este link não pode concluir o cadastro.",
    "identidade_bloqueada": "Não foi possível concluir este cadastro.",
    "identidade_revogada": "Não foi possível concluir este cadastro.",
    "identidade_indisponivel": "Não foi possível concluir este cadastro.",
    "senha_invalida": "A senha precisa ter no mínimo 8 caracteres.",
    "senha_divergente": "As senhas não conferem.",
    "vinculo_conta_divergente": "Esta conta não corresponde ao e-mail do cadastro.",
    "autenticacao_necessaria": "Entre com a conta deste e-mail para conectar o canal.",
    "existing_account_verification_required": "Este e-mail já tem conta. Entre e confirme a conexão.",
    "vinculo_nao_aplicavel": "Este link não pode conectar a conta.",
    "conclusao_conflito": "Não foi possível concluir agora. Tente novamente.",
    "falha_interna": "Não foi possível concluir agora. Tente novamente.",
    "conta_criada": "Conta criada. Você já pode voltar ao WhatsApp.",
    "vinculo_confirmado": "Conta conectada ao canal. Você já pode voltar ao WhatsApp.",
}

_FORMULARIO = {CODIGO_LINK_EMITIDO, CODIGO_CONTA_EXISTENTE, CODIGO_AUTH_NECESSARIA}
_SUCESSO = {CODIGO_CONTA_CRIADA, CODIGO_VINCULO_CONFIRMADO}


def register_onboarding_canal_routes(app) -> None:
    app.add_url_rule(
        "/c/<alias>",
        endpoint="onboarding_canal_alias",
        view_func=onboarding_canal_alias,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/onboarding/canal/concluir/<token>",
        endpoint="onboarding_canal_concluir",
        view_func=onboarding_canal_concluir,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        CAMINHO_CONTINUAR,
        endpoint="onboarding_canal_continuar",
        view_func=onboarding_canal_continuar,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        "/onboarding/canal/retorno",
        endpoint="onboarding_canal_retorno",
        view_func=onboarding_canal_retorno,
        methods=["GET"],
    )


def ajustar_next_login_onboarding_canal(nxt: str | None) -> str | None:
    """Tira o token de conclusão do destino de login. O handoff fica só na sessão."""
    if not nxt:
        return None
    path = urlsplit(nxt).path or ""
    alias_no_login = path.startswith("/c/") and "/" not in path[3:] and path != "/c/"
    if path == "/onboarding/canal/concluir" or path.startswith(_PREFIXO_LINK_CONCLUSAO) or alias_no_login:
        if _id_handoff(session.get(SESSION_HANDOFF_ID)) is not None:
            return CAMINHO_CONTINUAR
        return None
    return nxt


def onboarding_canal_alias(alias: str):
    """Conclusão pelo alias curto. O token assinado não volta para a barra."""
    return _pagina_conclusao(
        inspecionar=lambda: inspecionar_alias_conclusao(alias),
        concluir_senha=lambda: concluir_definicao_senha_por_alias(
            alias,
            request.form.get("password") or "",
            request.form.get("confirm_password") or "",
        ),
        confirmar_vinculo=lambda usuario: confirmar_vinculo_conta_por_alias(alias, usuario),
    )


def onboarding_canal_concluir(token: str):
    secret_key = current_app.config["SECRET_KEY"]
    return _pagina_conclusao(
        inspecionar=lambda: inspecionar_link_conclusao(token, secret_key=secret_key),
        concluir_senha=lambda: concluir_definicao_senha(
            token,
            request.form.get("password") or "",
            request.form.get("confirm_password") or "",
            secret_key=secret_key,
        ),
        confirmar_vinculo=lambda usuario: confirmar_vinculo_conta_existente(
            token,
            usuario,
            secret_key=secret_key,
        ),
    )


def _pagina_conclusao(*, inspecionar, concluir_senha, confirmar_vinculo):
    erro = None
    sucesso = False
    if request.method == "POST":
        acao = (request.form.get("acao") or "definir_senha").strip()
        if acao == "vincular":
            usuario = current_user if getattr(current_user, "is_authenticated", False) else None
            resultado = confirmar_vinculo(usuario)
        else:
            resultado = concluir_senha()
        if resultado.autenticar and resultado.user_id:
            novo = db.session.get(User, resultado.user_id)
            if novo is not None:
                login_user(novo)
        if resultado.codigo in _SUCESSO or resultado.codigo == CODIGO_JA_REALIZADA:
            _limpar_handoff()
            return _render(resultado.codigo, sucesso=resultado.codigo in _SUCESSO)
        if resultado.codigo in _FORMULARIO or resultado.codigo in {
            CODIGO_SENHA_INVALIDA,
            CODIGO_SENHA_DIVERGENTE,
            CODIGO_VINCULO_DIVERGENTE,
        }:
            erro = _mensagem(resultado.codigo, request.form.get("password") or "")
        else:
            return _render(resultado.codigo, erro=_mensagem(resultado.codigo, ""))
    estado = inspecionar()
    if (
        estado.formulario == "vinculo"
        and estado.codigo in _FORMULARIO
        and not _autenticado()
    ):
        if _registrar_handoff(estado.onboarding_id):
            return redirect(url_for("login", next=CAMINHO_CONTINUAR))
    if estado.codigo not in _FORMULARIO and erro is None:
        return _render(estado.codigo)
    return _render(
        estado.codigo,
        erro=erro,
        formulario=estado.formulario,
        nome_mascarado=estado.nome_mascarado,
        email_mascarado=estado.email_mascarado,
        sucesso=sucesso,
    )


def onboarding_canal_continuar():
    """Retorno pós-login. A sessão só tem o id da conclusão, nunca o token."""
    if not _autenticado():
        return redirect(url_for("login", next=CAMINHO_CONTINUAR))
    handoff_id = _id_handoff(session.get(SESSION_HANDOFF_ID))
    if handoff_id is None:
        return _render("token_invalido")
    if request.method == "POST" and (request.form.get("acao") or "").strip() == "vincular":
        resultado = confirmar_vinculo_handoff(handoff_id, current_user)
        if resultado.codigo in _SUCESSO or resultado.codigo == CODIGO_JA_REALIZADA:
            _limpar_handoff()
            return _render(resultado.codigo, sucesso=resultado.codigo in _SUCESSO)
        if resultado.codigo == CODIGO_VINCULO_DIVERGENTE:
            estado = inspecionar_handoff_conclusao(handoff_id)
            return _render(
                resultado.codigo,
                erro=_mensagem(resultado.codigo, ""),
                formulario="vinculo",
                nome_mascarado=estado.nome_mascarado,
                email_mascarado=estado.email_mascarado,
            )
        _limpar_handoff()
        return _render(resultado.codigo, erro=_mensagem(resultado.codigo, ""))
    estado = inspecionar_handoff_conclusao(handoff_id)
    if estado.codigo not in _FORMULARIO or estado.formulario != "vinculo":
        _limpar_handoff()
        return _render(estado.codigo)
    return _render(
        estado.codigo,
        formulario="vinculo",
        nome_mascarado=estado.nome_mascarado,
        email_mascarado=estado.email_mascarado,
    )


def onboarding_canal_retorno():
    """Retorno fixo ao canal. Ignora query string: não há redirect aberto."""
    destino = url_retorno_canal_permitida()
    if destino:
        return redirect(destino)
    return render_template("onboarding_canal_retorno.html")


def _render(
    codigo: str,
    *,
    erro: str | None = None,
    formulario: str | None = None,
    nome_mascarado: str | None = None,
    email_mascarado: str | None = None,
    sucesso: bool = False,
):
    login_url = None
    if formulario == "vinculo" and not _autenticado():
        login_url = url_for("login", next=CAMINHO_CONTINUAR)
    return render_template(
        "onboarding_canal_concluir.html",
        codigo=codigo,
        mensagem=erro or _mensagem(codigo, ""),
        alerta=bool(erro),
        formulario=formulario,
        nome_mascarado=nome_mascarado,
        email_mascarado=email_mascarado,
        sucesso=sucesso,
        login_url=login_url,
        autenticado=bool(getattr(current_user, "is_authenticated", False)),
    )


def _autenticado() -> bool:
    return bool(getattr(current_user, "is_authenticated", False))


def _id_handoff(bruto) -> int | None:
    if isinstance(bruto, bool) or not isinstance(bruto, int) or bruto <= 0:
        return None
    return bruto


def _registrar_handoff(onboarding_id: int | None) -> bool:
    if isinstance(onboarding_id, bool) or not isinstance(onboarding_id, int) or onboarding_id <= 0:
        return False
    row = (
        OnboardingCanalConclusao.query.filter_by(
            onboarding_id=onboarding_id,
            estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        )
        .order_by(OnboardingCanalConclusao.id.desc())
        .first()
    )
    if row is None:
        return False
    session[SESSION_HANDOFF_ID] = int(row.id)
    return True


def _limpar_handoff() -> None:
    session.pop(SESSION_HANDOFF_ID, None)


def _mensagem(codigo: str, password: str) -> str:
    if codigo == CODIGO_SENHA_INVALIDA:
        return validar_senha_cadastro(password) or _MENSAGENS["senha_invalida"]
    if codigo == CODIGO_SENHA_DIVERGENTE:
        return _MENSAGENS["senha_divergente"]
    return _MENSAGENS.get(codigo, "Não foi possível concluir este cadastro.")
