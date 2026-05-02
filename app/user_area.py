import logging
import os
import time
from datetime import datetime, timezone
from flask import Blueprint, jsonify, render_template, redirect, url_for, request, session
from flask_login import login_required, current_user, logout_user

from app.models import User
from app.auth_services import encerrar_contrato
from app.services.cleiton_monetizacao_service import (
    conciliar_checkout_session_stripe,
    iniciar_jornada_assinatura_stripe,
    listar_planos_contratacao_publica,
    obter_pendencia_downgrade_conta_ativa,
)

logger = logging.getLogger(__name__)
HIERARQUIA_PLANOS = {"free": 0, "starter": 1, "pro": 2}

# Contexto leve da jornada embedded (evita falso positivo de UX com pendencia antiga).
_SESSION_CONTRATACAO_EMBED_PLANO = "contratacao_embed_plano_alvo"
_SESSION_CONTRATACAO_EMBED_EPOCH = "contratacao_embed_inicio_epoch"
_JANELA_FEEDBACK_PENDENCIA_SEG = 20 * 60
_JANELA_SESSAO_CONTRATACAO_SEG = 2 * 3600


base_dir = os.path.dirname(os.path.abspath(__file__))


def _limpar_contexto_sessao_contratacao_embed() -> None:
    session.pop(_SESSION_CONTRATACAO_EMBED_PLANO, None)
    session.pop(_SESSION_CONTRATACAO_EMBED_EPOCH, None)


def _pendencia_downgrade_elegivel_para_feedback_retorno(pend_fb: dict | None) -> bool:
    """
    Pendencia ativa so alimenta mensagem de retorno com evidencia de jornada recente:
    plano-alvo da sessao coincide com a pendencia (checkout iniciado nesta sessao), ou
    registro da pendencia atualizado na ultima janela curta (ex.: webhook + redirect sem session_id).
    """
    if not pend_fb or not pend_fb.get("plano_pendente"):
        return False
    pend_plano = (pend_fb.get("plano_pendente") or "").strip().lower()
    plano_sess = (session.get(_SESSION_CONTRATACAO_EMBED_PLANO) or "").strip().lower()
    epoch_raw = session.get(_SESSION_CONTRATACAO_EMBED_EPOCH)
    try:
        epoch_i = int(epoch_raw) if epoch_raw is not None else None
    except (TypeError, ValueError):
        epoch_i = None
    now_ts = time.time()
    if plano_sess and pend_plano == plano_sess and epoch_i is not None:
        if 0 <= now_ts - epoch_i <= _JANELA_SESSAO_CONTRATACAO_SEG:
            return True
    atxt = (pend_fb.get("atualizado_em") or "").strip()
    if not atxt:
        return False
    try:
        dt = datetime.fromisoformat(atxt.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        ref = datetime.now(timezone.utc).replace(tzinfo=None)
        age_sec = (ref - dt).total_seconds()
        if 0 <= age_sec <= _JANELA_FEEDBACK_PENDENCIA_SEG:
            return True
    except Exception:
        logger.warning(
            "[StripeDebug][Retorno] atualizado_em da pendencia invalido raw=%s",
            atxt,
        )
    return False


def _obter_pendencia_downgrade_com_seguranca(conta_id: int | None) -> dict | None:
    try:
        return obter_pendencia_downgrade_conta_ativa(conta_id)
    except Exception:
        logger.exception(
            "[StripeDebug][Retorno] obter_pendencia_downgrade_conta_ativa levantou excecao conta_id=%s",
            conta_id,
        )
        return None


def _checkout_feedback_downgrade_pendente(
    *,
    plano_atual: str,
    plano_pendente: str,
    efetivar_em: str,
    data_vencimento_iso: str | None,
) -> dict[str, str]:
    plano_pendente_l = (plano_pendente or "").strip().lower()
    efetivar = (efetivar_em or data_vencimento_iso or "").strip()
    if plano_pendente_l == "free":
        return {
            "nivel": "success",
            "mensagem": (
                f"Sua assinatura do plano {plano_atual.capitalize()} foi cancelada. "
                f"Essa alteracao entrara em vigor em {efetivar}."
            ),
        }
    return {
        "nivel": "success",
        "mensagem": (
            f"Sua alteracao para o plano {plano_pendente_l.capitalize()} foi registrada. "
            f"Ela entrara em vigor em {efetivar}."
        ),
    }


user_bp = Blueprint(
    "user",
    __name__,
    template_folder=os.path.join(base_dir, "templates"),
)


@user_bp.route("/perfil")
@login_required
def perfil():
    """
    Área do Usuário autenticado.

    A lógica de segurança (login_required) e de autorização adicional
    permanece delegada ao Flask-Login e ao modelo User, evitando hardcodes.
    """
    assert isinstance(current_user._get_current_object(), User)  # type: ignore[attr-defined]
    return render_template("user_area.html")


@user_bp.route("/contrate-um-plano")
@login_required
def contrate_plano():
    planos = listar_planos_contratacao_publica()
    user_obj = current_user._get_current_object()
    plano_atual = ((getattr(user_obj, "categoria", None) or "free").strip().lower())
    if plano_atual not in HIERARQUIA_PLANOS:
        plano_atual = "free"
    franquia = getattr(user_obj, "franquia", None)
    data_vencimento = getattr(franquia, "fim_ciclo", None)
    data_vencimento_iso = data_vencimento.isoformat() if data_vencimento is not None else None
    planos_filtrados = list(planos)
    if plano_atual == "free":
        planos_filtrados = [p for p in planos if (p.get("codigo") or "").strip().lower() != "free"]
    checkout_feedback = None
    pixel_subscribe_event = None
    checkout_flag = (request.args.get("checkout") or "").strip().lower()
    session_id = (request.args.get("session_id") or "").strip()
    session_id_valido = bool(session_id) and "CHECKOUT_SESSION_ID" not in session_id
    logging.info(
        "[StripeDebug][Retorno] Entrada contrate_plano checkout_flag=%s session_id=%s current_user_id=%s current_user_categoria=%s",
        checkout_flag,
        session_id,
        getattr(current_user, "id", None),
        getattr(current_user, "categoria", None),
    )
    if checkout_flag == "success" and session_id_valido:
        try:
            logging.info(
                "[StripeDebug][Retorno] Antes conciliacao checkout session_id=%s current_user_id=%s",
                session_id,
                getattr(current_user, "id", None),
            )
            resultado = conciliar_checkout_session_stripe(session_id)
            logging.info(
                "[StripeDebug][Retorno] Resultado conciliacao checkout session_id=%s current_user_id=%s resultado=%s",
                session_id,
                getattr(current_user, "id", None),
                resultado,
            )
            if (
                resultado.get("efeito_operacional_aplicado")
                and not resultado.get("replay")
                and not resultado.get("mudanca_pendente")
            ):
                _limpar_contexto_sessao_contratacao_embed()
                checkout_feedback = {
                    "nivel": "success",
                    "mensagem": "Contratacao confirmada com sucesso. Seu plano pago ja foi refletido no sistema.",
                }
                pixel_subscribe_event = {
                    "event_name": "Subscribe",
                    "session_id": session_id,
                }
            elif resultado.get("mudanca_pendente") and resultado.get("plano_pendente"):
                _limpar_contexto_sessao_contratacao_embed()
                plano_pendente = (resultado.get("plano_pendente") or "").strip().lower()
                efetivar_em = (resultado.get("efetivar_em") or data_vencimento_iso or "").strip()
                checkout_feedback = _checkout_feedback_downgrade_pendente(
                    plano_atual=plano_atual,
                    plano_pendente=plano_pendente,
                    efetivar_em=efetivar_em,
                    data_vencimento_iso=data_vencimento_iso,
                )
            elif resultado.get("replay"):
                _limpar_contexto_sessao_contratacao_embed()
                checkout_feedback = {
                    "nivel": "success",
                    "mensagem": "Contratacao ja conciliada anteriormente. Seu contexto de plano foi mantido sem duplicidade.",
                }
            elif resultado.get("conciliado") is False:
                pend_fb = _obter_pendencia_downgrade_com_seguranca(getattr(user_obj, "conta_id", None))
                if pend_fb and _pendencia_downgrade_elegivel_para_feedback_retorno(pend_fb):
                    checkout_feedback = _checkout_feedback_downgrade_pendente(
                        plano_atual=plano_atual,
                        plano_pendente=str(pend_fb.get("plano_pendente") or ""),
                        efetivar_em=str(pend_fb.get("efetivar_em") or ""),
                        data_vencimento_iso=data_vencimento_iso,
                    )
                else:
                    checkout_feedback = {
                        "nivel": "secondary",
                        "mensagem": "Checkout retornou, mas o pagamento ainda nao estava confirmado para conciliacao imediata.",
                    }
            else:
                pend_fb = _obter_pendencia_downgrade_com_seguranca(getattr(user_obj, "conta_id", None))
                if pend_fb and _pendencia_downgrade_elegivel_para_feedback_retorno(pend_fb):
                    checkout_feedback = _checkout_feedback_downgrade_pendente(
                        plano_atual=plano_atual,
                        plano_pendente=str(pend_fb.get("plano_pendente") or ""),
                        efetivar_em=str(pend_fb.get("efetivar_em") or ""),
                        data_vencimento_iso=data_vencimento_iso,
                    )
                else:
                    checkout_feedback = {
                        "nivel": "secondary",
                        "mensagem": "Pagamento recebido com sucesso e ativacao em processamento. O plano sera refletido automaticamente apos a conciliacao dos eventos Stripe.",
                    }
        except Exception as exc:
            logging.exception(
                "[StripeDebug][Retorno] Excecao na conciliacao checkout tipo=%s mensagem=%s session_id=%s current_user_id=%s",
                type(exc).__name__,
                exc,
                session_id,
                getattr(current_user, "id", None),
            )
            checkout_feedback = {
                "nivel": "warning",
                "mensagem": "Pagamento retornou do Stripe, mas nao foi possivel confirmar a ativacao interna neste momento.",
            }
    elif checkout_flag == "success":
        pend_fb = _obter_pendencia_downgrade_com_seguranca(getattr(user_obj, "conta_id", None))
        if pend_fb and _pendencia_downgrade_elegivel_para_feedback_retorno(pend_fb):
            checkout_feedback = _checkout_feedback_downgrade_pendente(
                plano_atual=plano_atual,
                plano_pendente=str(pend_fb.get("plano_pendente") or ""),
                efetivar_em=str(pend_fb.get("efetivar_em") or ""),
                data_vencimento_iso=data_vencimento_iso,
            )
        else:
            checkout_feedback = {
                "nivel": "secondary",
                "mensagem": "Pagamento recebido com sucesso e ativacao em processamento. O plano sera refletido automaticamente apos a conciliacao dos eventos Stripe.",
            }
    elif checkout_flag == "cancelled":
        checkout_feedback = {
            "nivel": "secondary",
            "mensagem": "Checkout cancelado. Nenhuma alteracao contratual foi aplicada.",
        }
    logging.info(
        "[StripeDebug][Retorno] Antes render contrate_plano checkout_feedback=%s current_user_id=%s current_user_categoria=%s",
        checkout_feedback,
        getattr(current_user, "id", None),
        getattr(current_user, "categoria", None),
    )
    return render_template(
        "contrate_plano.html",
        planos_contratacao=planos_filtrados,
        checkout_feedback=checkout_feedback,
        plano_atual_codigo=plano_atual,
        data_vencimento_atual=data_vencimento_iso,
        pixel_subscribe_event=pixel_subscribe_event,
    )


def _extrair_plano_codigo_contratacao() -> str:
    """
    Resolve plano_codigo com prioridade para JSON, mantendo fallback para form/query
    para clientes não-frontend.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    plano_codigo = (payload.get("plano_codigo") or "").strip()
    if not plano_codigo:
        plano_codigo = (request.form.get("plano_codigo") or "").strip()
    if not plano_codigo:
        plano_codigo = (request.args.get("plano_codigo") or "").strip()
    return plano_codigo.lower()


def _extrair_confirmacao_downgrade() -> bool:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    raw = payload.get("confirmar_downgrade")
    if raw is None:
        raw = request.form.get("confirmar_downgrade")
    if raw is None:
        raw = request.args.get("confirmar_downgrade")
    return str(raw).strip().lower() in {"1", "true", "sim", "yes", "on"}


def _erro_contratacao_payload(erro: str, codigo_erro: str) -> dict:
    """
    Mantém `erro` legado e adiciona metadados de contrato sem breaking change.
    """
    return {
        "ok": False,
        "erro": erro,
        "codigo_erro": codigo_erro,
        "error": erro,  # alias retrocompatível para consumidores não padronizados.
    }


@user_bp.route("/api/contratacao/stripe/iniciar", methods=["POST"])
@login_required
def iniciar_contratacao_stripe():
    """
    Endpoint oficial autenticado para iniciar jornada de contratacao Stripe.
    """
    plano_codigo = _extrair_plano_codigo_contratacao()
    if not plano_codigo:
        return jsonify(
            _erro_contratacao_payload(
                "plano_codigo_obrigatorio",
                "contratacao_stripe_plano_codigo_obrigatorio",
            )
        ), 400
    user = current_user._get_current_object()
    if not isinstance(user, User):
        return jsonify(
            _erro_contratacao_payload(
                "usuario_invalido",
                "contratacao_stripe_usuario_invalido",
            )
        ), 401
    plano_atual = ((getattr(user, "categoria", None) or "free").strip().lower())
    if plano_atual not in HIERARQUIA_PLANOS:
        plano_atual = "free"
    confirmar_downgrade = _extrair_confirmacao_downgrade()
    if (
        plano_codigo in HIERARQUIA_PLANOS
        and HIERARQUIA_PLANOS.get(plano_codigo, -1) < HIERARQUIA_PLANOS.get(plano_atual, -1)
        and not confirmar_downgrade
    ):
        return jsonify(
            _erro_contratacao_payload(
                "confirmacao_downgrade_obrigatoria",
                "contratacao_stripe_confirmacao_downgrade_obrigatoria",
            )
        ), 400
    try:
        site_origin = request.host_url.rstrip("/")
        out = iniciar_jornada_assinatura_stripe(
            user=user,
            plano_codigo=plano_codigo,
            site_origin=site_origin,
        )
        if out.get("checkout_client_secret"):
            session[_SESSION_CONTRATACAO_EMBED_PLANO] = plano_codigo
            session[_SESSION_CONTRATACAO_EMBED_EPOCH] = int(time.time())
        return jsonify({"ok": True, **out})
    except ValueError as exc:
        return jsonify(
            _erro_contratacao_payload(
                str(exc),
                "contratacao_stripe_requisicao_invalida",
            )
        ), 400
    except Exception as exc:
        logger.exception(
            "Falha ao iniciar contratacao Stripe (user_id=%s, plano=%s): %s",
            getattr(user, "id", None),
            plano_codigo,
            exc,
        )
        return jsonify(
            _erro_contratacao_payload(
                "falha_interna_inicio_contratacao",
                "contratacao_stripe_falha_interna",
            )
        ), 500


@user_bp.route("/perfil/encerrar-contrato", methods=["POST"])
@login_required
def encerrar_contrato_route():
    """
    Encerra o contrato do usuário: anonimiza dados, invalida sessão e redireciona.
    Deve ser chamado após confirmação no modal (POST).
    """
    user = current_user._get_current_object()
    if not isinstance(user, User):
        return redirect(url_for("user.perfil"))
    encerrar_contrato(user)
    logout_user()
    return redirect(url_for("index"))

