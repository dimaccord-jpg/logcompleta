"""
Rotas do painel administrativo.
Apenas definicao de rotas, autenticacao, autorizacao e renderizacao de templates.
Toda logica de negocio esta em app.services e app.tasks.
"""
from flask import (
    Blueprint,
    render_template,
    flash,
    redirect,
    url_for,
    request,
    current_app,
    send_file,
    jsonify,
    Response,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
import os
import io
import logging
import threading
from concurrent.futures import Future
from datetime import datetime, date, timezone

from app.infra import (
    get_admin_executor,
    user_is_admin,
    get_julia_chat_max_history,
)
from app.consumo_identidade import capture_consumo_identidade_for_background
from app.terms_services import get_active_term
from app.privacy_policy_services import get_active_privacy_policy
from app.finance import (
    atualizar_indices,
    configurar_finance_frequencia_horas,
    configurar_finance_frequencia_minutos,
    obter_finance_frequencia_horas,
    obter_finance_frequencia_minutos,
)
from app.run_julia_regras import status_verificacao_permitidos

from app.services import agent_service
from app.services import pauta_service
from app.services import serie_service
from app.services import import_service
from app.services import plano_service
from app.services import termo_service
from app.services import privacy_policy_service
from app.services import auditoria_service
from app.services import user_admin_control_service
from app.services import user_plan_control_service

from app.tasks import agent_tasks
from app.tasks import import_tasks

base_dir = os.path.dirname(os.path.abspath(__file__))
pasta_templates = os.path.join(base_dir, "template_admin")

admin_bp = Blueprint(
    "admin",
    __name__,
    template_folder=pasta_templates,
    url_prefix="/admin",
)


def _admin_app_env() -> str:
    return (os.getenv("APP_ENV") or "dev").strip().lower()


def _min_frequencia_minutos_para_admin() -> int:
    return 60 if _admin_app_env() == "prod" else 5


def _validar_frequencia_minutos_admin(valor_raw: str, *, contexto: str) -> int:
    try:
        valor = int((valor_raw or "").strip())
    except (ValueError, TypeError):
        raise ValueError(f"Informe minutos inteiros para {contexto}.")
    minimo = _min_frequencia_minutos_para_admin()
    if valor < minimo:
        if minimo >= 60:
            raise ValueError(
                f"Em producao, a frequencia minima para {contexto} e de 60 minutos."
            )
        raise ValueError(
            f"Em {_admin_app_env()}, a frequencia minima para {contexto} e de {minimo} minutos."
        )
    return valor


def _formatar_frequencia_minutos(valor: int) -> str:
    minutos = max(1, int(valor))
    if minutos % 60 == 0:
        horas = minutos // 60
        return f"{horas}h"
    return f"{minutos} min"

# Estado para execucao async (Cleiton / artigo manual)
_CLEITON_FUTURE: Future | None = None
_ARTIGO_MANUAL_FUTURE: Future | None = None
_CLEITON_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


def verificar_acesso_admin():
    """Retorna True se o usuário estiver autenticado e is_admin for explicitamente True."""
    return current_user.is_authenticated and user_is_admin(current_user)


def _erro_admin_validacao_payload(erro: str, codigo_erro: str) -> dict:
    """
    Preserva chaves legadas (`error`/`erro`) e adiciona código estável.
    """
    return {
        "ok": False,
        "erro": erro,
        "error": erro,
        "codigo_erro": codigo_erro,
    }


# --- Dashboard ---
@admin_bp.route("/")
@admin_bp.route("/dashboard")
@login_required
def admin_dashboard():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.services.admin_dashboard_service import (
        get_dashboard_metrics,
        list_categorias_distintas,
        list_franquia_status_distintos,
    )

    categoria_f = (request.args.get("categoria") or "").strip() or None
    franquia_status_f = (request.args.get("franquia_status") or "").strip() or None
    cancelado_f = (request.args.get("cancelado") or "ativos").strip().lower()

    dash_metrics = get_dashboard_metrics(
        categoria=categoria_f,
        franquia_status=franquia_status_f,
        cancelado=cancelado_f,
    )
    kpis_insight = agent_service.obter_kpis_insight()
    recomendacoes_recentes = agent_service.obter_recomendacoes_recentes(
        limite=15
    )
    from app.services.ia_metrics_service import get_ia_dashboard_payload

    _today = date.today()
    ia_metrics = get_ia_dashboard_payload(_today.year, _today.month)
    return render_template(
        "dashboard.html",
        dash_metrics=dash_metrics,
        filtros_categorias=list_categorias_distintas(),
        filtros_franquia_status=list_franquia_status_distintos(),
        filtro_categoria=categoria_f or "",
        filtro_franquia_status=franquia_status_f or "",
        filtro_cancelado=cancelado_f,
        kpis_insight=kpis_insight,
        recomendacoes_recentes=recomendacoes_recentes,
        ia_metrics=ia_metrics,
    )


@admin_bp.route("/dashboard/auditoria-clientes.csv")
@login_required
def admin_dashboard_auditoria_clientes_csv():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.services.admin_auditoria_clientes_csv_service import (
        gerar_csv_auditoria_clientes,
    )

    categoria_f = (request.args.get("categoria") or "").strip() or None
    franquia_status_f = (request.args.get("franquia_status") or "").strip() or None
    cancelado_f = (request.args.get("cancelado") or "ativos").strip().lower()
    filtros = {
        "categoria": categoria_f,
        "franquia_status": franquia_status_f,
        "cancelado": cancelado_f,
    }
    csv_payload, total_exportado = gerar_csv_auditoria_clientes(filtros)
    nome_arquivo = f"auditoria_clientes_admin_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        auditoria_service.registrar_auditoria_admin(
            actor_email=getattr(current_user, "email", None),
            tipo_decisao="admin_operacao",
            decisao="download_csv_auditoria_clientes_dashboard",
            entidade="admin_dashboard",
            entidade_id=getattr(current_user, "id", None),
            estado_antes=None,
            estado_depois={
                "filtros": filtros,
                "total_exportado": int(total_exportado),
                "arquivo": nome_arquivo,
            },
            motivo="export_csv_auditoria_local_readonly",
            resultado="sucesso",
        )
    except Exception:
        _log.exception("Falha ao registrar auditoria de download do CSV")

    resp = Response(csv_payload, content_type="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resp


# --- Metricas de IA (fase 1: tokens + custo GCP) ---
@admin_bp.route("/api/ia-metrics")
@login_required
def admin_api_ia_metrics():
    """JSON: totais de tokens no mes, por API key, custo do snapshot e custo/token."""
    if not verificar_acesso_admin():
        return jsonify({"error": "forbidden"}), 403
    y = request.args.get("year", type=int)
    mo = request.args.get("month", type=int)
    if not y or not mo:
        today = date.today()
        y, mo = today.year, today.month
    from app.services.ia_metrics_service import get_ia_dashboard_payload

    return jsonify(get_ia_dashboard_payload(y, mo))


@admin_bp.route(
    "/api/cleiton-franquia/<int:franquia_id>/validacao",
    methods=["GET", "POST"],
)
@login_required
def admin_api_cleiton_franquia_validacao(franquia_id):
    """
    JSON: leitura operacional Cleiton, reconciliação consumo vs eventos e pendências.
    GET: somente consulta. POST: opcional `aplicar_correcao` (query ou JSON) para alinhar consumo persistido.
    Query: sincronizar_ciclo=1, aplicar_correcao=1 (POST).
    """
    if not verificar_acesso_admin():
        return jsonify(
            _erro_admin_validacao_payload("forbidden", "admin_validacao_forbidden")
        ), 403
    sinc = request.args.get("sincronizar_ciclo", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    payload_raw = request.get_json(silent=True)
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    aplicar = False
    try:
        acao = ""
        if request.method == "POST":
            acao = (payload.get("acao") or "").strip().lower()
        if acao == "reprocessar_pendencias_correlacao":
            from app.services.cleiton_franquia_validacao_admin_service import (
                reprocessar_pendencias_monetizacao_franquia_admin,
            )

            limite = payload.get("limite", 20)
            try:
                limite = int(limite)
            except (TypeError, ValueError):
                limite = 20
            admin_user_id = getattr(current_user, "id", None)
            return jsonify(
                reprocessar_pendencias_monetizacao_franquia_admin(
                    franquia_id=franquia_id,
                    admin_user_id=int(admin_user_id) if admin_user_id is not None else None,
                    limite=limite,
                )
            )
        if request.method == "POST":
            aplicar = request.args.get("aplicar_correcao", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            aplicar = aplicar or bool(payload.get("aplicar_correcao"))
        from app.services.cleiton_franquia_validacao_admin_service import (
            obter_pacote_validacao_franquia_cleiton,
        )

        return jsonify(
            obter_pacote_validacao_franquia_cleiton(
                franquia_id,
                sincronizar_ciclo_leitura=sinc,
                aplicar_correcao=aplicar,
            )
        )
    except ValueError as exc:
        _log.warning(
            "Erro de validacao no endpoint admin de franquia %s: %s",
            franquia_id,
            exc,
        )
        return jsonify(
            _erro_admin_validacao_payload(
                str(exc),
                "admin_validacao_requisicao_invalida",
            )
        ), 400
    except Exception as exc:
        _log.exception(
            "Falha no endpoint admin de validacao da franquia %s: %s",
            franquia_id,
            exc,
        )
        return jsonify(
            _erro_admin_validacao_payload(
                "falha_interna_validacao_admin",
                "admin_validacao_falha_interna",
            )
        ), 500


# --- Agentes ---
@admin_bp.route("/agentes")
@login_required
def agentes_home():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return redirect(url_for("admin.agentes_julia"))


@admin_bp.route("/agentes/julia")
@login_required
def agentes_julia():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    frequencia_minutos = agent_service.obter_frequencia_minutos()
    ultima_execucao, proxima_prevista = (
        agent_service.obter_ultima_e_proxima_execucao(frequencia_minutos)
    )
    janela_inicio, janela_fim = agent_service.obter_janela_publicacao()
    status_pautas_artigo = agent_service.obter_status_pautas_artigo()
    ultima_artigo = agent_service.obter_ultima_publicacao_artigo()
    ultima_execucao_manual = agent_service.ler_ultima_execucao_manual()
    return render_template(
        "agentes_julia.html",
        frequencia_horas=agent_service.obter_frequencia_horas(),
        frequencia_minutos=frequencia_minutos,
        frequencia_label=_formatar_frequencia_minutos(frequencia_minutos),
        frequencia_minima_admin=_min_frequencia_minutos_para_admin(),
        julia_chat_max_history=get_julia_chat_max_history(),
        ultima_execucao=ultima_execucao,
        proxima_prevista=proxima_prevista,
        janela_inicio=janela_inicio,
        janela_fim=janela_fim,
        status_pautas_artigo=status_pautas_artigo,
        ultima_artigo=ultima_artigo,
        ultima_execucao_manual=ultima_execucao_manual,
    )


@admin_bp.route("/agentes/julia/frequencia", methods=["POST"])
@login_required
def agentes_julia_configurar_frequencia():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    valor_raw = (request.form.get("frequencia_minutos") or "").strip()
    try:
        valor = _validar_frequencia_minutos_admin(
            valor_raw,
            contexto="o ciclo da Julia/Cleiton",
        )
    except ValueError as e:
        flash(f"Valor de frequencia invalido. {str(e)}", "warning")
        return redirect(url_for("admin.agentes_julia"))
    try:
        agent_service.configurar_frequencia_minutos(valor)
        flash(
            f"Frequencia do ciclo atualizada para {_formatar_frequencia_minutos(valor)}.",
            "success",
        )
    except Exception as e:
        flash(f"Erro ao atualizar frequencia: {str(e)}", "danger")
    return redirect(url_for("admin.agentes_julia"))
    try:
        valor = int(valor_raw)
        if valor < 1:
            raise ValueError("Frequência deve ser maior que zero.")
    except (ValueError, TypeError):
        flash(
            "Valor de frequência inválido. Informe horas inteiras (ex.: 1, 3, 6).",
            "warning",
        )
        return redirect(url_for("admin.agentes_julia"))
    try:
        agent_service.configurar_frequencia_horas(valor)
        flash(f"Frequência do ciclo atualizada para {valor}h.", "success")
    except Exception as e:
        flash(f"Erro ao atualizar frequência: {str(e)}", "danger")
    return redirect(url_for("admin.agentes_julia"))


@admin_bp.route("/agentes/julia/historico", methods=["POST"])
@login_required
def agentes_julia_configurar_historico():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    raw_history = (request.form.get("julia_chat_max_history") or "").strip()
    try:
        novo_valor = plano_service.salvar_julia_chat_max_history(
            raw_history or None
        )
        if novo_valor is None:
            flash(
                "Informe um limite de histórico válido (1 a 100).",
                "warning",
            )
        else:
            flash(f"Limite de histórico da Júlia atualizado para {novo_valor}.", "success")
    except Exception:
        flash("Erro ao salvar limite de histórico. Tente novamente.", "danger")
    return redirect(url_for("admin.agentes_julia"))


@admin_bp.route("/agentes/roberto", methods=["GET", "POST"])
@login_required
def agentes_roberto():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.services import roberto_config_service

    if request.method == "POST":
        campos = {
            "upload_total_max": (request.form.get("upload_total_max") or "").strip(),
            "previsao_meses": (request.form.get("previsao_meses") or "").strip(),
            "min_linhas_mes_modelo": (request.form.get("min_linhas_mes_modelo") or "").strip(),
            "min_linhas_uf_heatmap_ranking": (
                request.form.get("min_linhas_uf_heatmap_ranking") or ""
            ).strip(),
            "max_pontos_dispersao": (request.form.get("max_pontos_dispersao") or "").strip(),
            "max_linhas_mes_modelo": (request.form.get("max_linhas_mes_modelo") or "").strip(),
            "max_linhas_uf_heatmap": (request.form.get("max_linhas_uf_heatmap") or "").strip(),
            "max_linhas_uf_ranking": (request.form.get("max_linhas_uf_ranking") or "").strip(),
            "upload_ttl_minutes": (request.form.get("upload_ttl_minutes") or "").strip(),
            "chat_max_history": (request.form.get("chat_max_history") or "").strip(),
        }
        try:
            roberto_config_service.salvar_roberto_config(campos)
            flash("Parâmetros do Roberto salvos com sucesso.", "success")
        except ValueError as e:
            flash(str(e), "warning")
        except Exception as e:
            _log.exception("Erro ao salvar parâmetros do Roberto: %s", e)
            flash("Não foi possível salvar os parâmetros do Roberto.", "danger")
        return redirect(url_for("admin.agentes_roberto"))

    cfg = roberto_config_service.get_roberto_config()
    return render_template(
        "agentes_roberto.html",
        cfg=cfg,
        defaults=roberto_config_service.DEFAULTS,
    )


@admin_bp.route("/agentes/cleiton", methods=["GET", "POST"])
@login_required
def agentes_cleiton():
    """Parâmetros operacionais Cleiton: custo (runtime/Google) e régua de conversão de créditos."""
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.services.cleiton_cost_service import (
        compute_cost_per_second,
        get_or_create_config,
        save_config,
    )

    if request.method == "POST":
        try:
            r_raw = (request.form.get("runtime_monthly_cost") or "").strip()
            runtime = float(r_raw.replace(",", ".")) if r_raw else None

            ms_raw = (request.form.get("month_seconds") or "").strip()
            month_seconds = int(ms_raw) if ms_raw else 2592000

            ap_raw = (request.form.get("allocation_percent") or "").strip()
            allocation = float(ap_raw.replace(",", ".")) if ap_raw else 1.0

            oh_raw = (request.form.get("overhead_factor") or "").strip()
            overhead = float(oh_raw.replace(",", ".")) if oh_raw else 1.0

            g_raw = (request.form.get("cost_per_million_tokens") or "").strip()
            google_ref = float(g_raw.replace(",", ".")) if g_raw else None

            ct_raw = (request.form.get("credit_tokens_per_credit") or "").strip()
            credit_tokens = float(ct_raw.replace(",", ".")) if ct_raw else None

            cl_raw = (request.form.get("credit_lines_per_credit") or "").strip()
            credit_lines = float(cl_raw.replace(",", ".")) if cl_raw else None

            cms_raw = (request.form.get("credit_ms_per_credit") or "").strip()
            credit_ms = float(cms_raw.replace(",", ".")) if cms_raw else None

            if month_seconds < 1:
                raise ValueError("month_seconds deve ser >= 1.")
            save_config(
                runtime_monthly_cost=runtime,
                month_seconds=month_seconds,
                allocation_percent=allocation,
                overhead_factor=overhead,
                cost_per_million_tokens=google_ref,
                credit_tokens_per_credit=credit_tokens,
                credit_lines_per_credit=credit_lines,
                credit_ms_per_credit=credit_ms,
            )
            flash("Parâmetros operacionais salvos.", "success")
        except (ValueError, TypeError) as e:
            flash(f"Valores invalidos: {e}", "danger")
        return redirect(url_for("admin.agentes_cleiton"))

    cfg = get_or_create_config()
    cps = compute_cost_per_second(cfg)
    return render_template(
        "agentes_cleiton.html",
        cfg=cfg,
        cost_per_second=cps,
    )


@admin_bp.route("/agentes/julia/executar-cleiton", methods=["POST"])
@login_required
def agentes_julia_executar_cleiton():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    bypass_frequencia = (request.form.get("bypass_frequencia") or "").strip() in (
        "1",
        "true",
        "True",
    )
    if agent_service.admin_exec_mode() == "async":
        global _CLEITON_FUTURE
        executor = get_admin_executor()
        with _CLEITON_LOCK:
            if _CLEITON_FUTURE and not _CLEITON_FUTURE.done():
                flash(
                    "Já existe uma execução do Cleiton em andamento. Aguarde a conclusão.",
                    "warning",
                )
                return redirect(url_for("admin.agentes_julia"))
            app_obj = current_app._get_current_object()
            _ident = capture_consumo_identidade_for_background()
            _CLEITON_FUTURE = executor.submit(
                agent_tasks.run_cleiton_background,
                app_obj,
                bypass_frequencia,
                _ident,
            )
        flash(
            "Execução do Cleiton iniciada em segundo plano. Acompanhe os logs para status final.",
            "info",
        )
        return redirect(url_for("admin.agentes_julia"))
    try:
        resultado = agent_service.executar_cleiton_sincrono(
            current_app, bypass_frequencia
        )
        agent_service.persistir_ultima_execucao_manual(
            resultado, "Executar Cleiton"
        )
        status = resultado.get("status") or "falha"
        mensagem = agent_service.formatar_mensagem_resultado_cleiton(resultado)
        if status == "sucesso":
            flash(mensagem, "success")
        elif status == "ignorado":
            flash(mensagem, "warning")
        else:
            flash(mensagem, "danger")
    except Exception as e:
        _log.exception(
            "Falha ao executar Cleiton via admin (bypass_frequencia=%s): %s",
            bypass_frequencia,
            e,
        )
        flash(f"Erro ao executar Cleiton: {str(e)}", "danger")
    return redirect(url_for("admin.agentes_julia"))


@admin_bp.route("/agentes/julia/executar-artigo-manual", methods=["POST"])
@login_required
def agentes_julia_executar_artigo_manual():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    if agent_service.admin_exec_mode() == "async":
        global _ARTIGO_MANUAL_FUTURE
        executor = get_admin_executor()
        with _CLEITON_LOCK:
            if _ARTIGO_MANUAL_FUTURE and not _ARTIGO_MANUAL_FUTURE.done():
                flash(
                    "Já existe uma execução manual de artigo em andamento. Aguarde a conclusão.",
                    "warning",
                )
                return redirect(url_for("admin.agentes_julia"))
            app_obj = current_app._get_current_object()
            _ident_art = capture_consumo_identidade_for_background()
            _ARTIGO_MANUAL_FUTURE = executor.submit(
                agent_tasks.run_artigo_manual_background,
                app_obj,
                _ident_art,
            )
        flash(
            "Execução manual de artigo iniciada em segundo plano. Acompanhe os logs para status final.",
            "info",
        )
        return redirect(url_for("admin.agentes_julia"))
    try:
        resultado = agent_service.executar_artigo_manual_sincrono(current_app)
        agent_service.persistir_ultima_execucao_manual(
            resultado, "Executar artigo agora"
        )
        status = resultado.get("status") or "falha"
        motivo = (
            resultado.get("motivo_final")
            or resultado.get("motivo")
            or "Execução manual de artigo concluída sem motivo detalhado."
        )
        caminho = resultado.get("caminho_usado")
        partes_msg = [motivo]
        if caminho:
            partes_msg.append(f"caminho_usado={caminho}")
        if resultado.get("mission_id"):
            partes_msg.append(f"mission_id={resultado.get('mission_id')}")
        msg_texto = " | ".join(partes_msg)
        link_em_proc = url_for(
            "admin.pautas_admin", tipo="artigo", status="em_processamento"
        )
        link_publicadas = url_for(
            "admin.pautas_admin", tipo="artigo", status="publicada"
        )
        mensagem = Markup(
            f"{escape(msg_texto)} | "
            f'<a href="{escape(link_em_proc)}">Acompanhar em processamento</a> | '
            f'<a href="{escape(link_publicadas)}">Acompanhar publicadas</a>'
        )
        if status == "sucesso":
            flash(mensagem, "success")
        elif status == "ignorado":
            flash(mensagem, "warning")
        else:
            flash(mensagem, "danger")
    except Exception as e:
        _log.exception("Falha ao executar artigo manual via admin: %s", e)
        flash(f"Erro ao executar artigo manual: {str(e)}", "danger")
    return redirect(url_for("admin.agentes_julia"))


@admin_bp.route("/agentes/julia/coletar-noticias", methods=["POST"])
@login_required
def agentes_julia_coletar_noticias():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        resultado_scout, resultado_verif = (
            agent_service.executar_coleta_noticias()
        )
        msg = (
            f"Coleta de notícias executada. "
            f"Inseridas: {resultado_scout.get('inseridas', 0)}, "
            f"Ignoradas (duplicatas/erros de inserção): {resultado_scout.get('ignoradas_duplicata', 0)}, "
            f"Fontes com erro: {resultado_scout.get('erros', 0)}. "
            f"Verificação: aprovadas={resultado_verif.get('aprovadas', 0)}, "
            f"revisar={resultado_verif.get('revisar', 0)}, "
            f"rejeitadas={resultado_verif.get('rejeitadas', 0)}."
        )
        msg_diag = (
            f" Fontes processadas: {resultado_scout.get('fontes_processadas', 0)}, "
            f"fontes com erro: {resultado_scout.get('fontes_com_erro', 0)}, "
            f"fontes sem itens: {resultado_scout.get('fontes_sem_itens', 0)}."
        )
        flash(msg + msg_diag, "success")
    except Exception as e:
        flash(f"Erro ao coletar/verificar notícias: {str(e)}", "danger")
    return redirect(url_for("admin.agentes_julia"))


# --- Recomendações ---
@admin_bp.route("/recomendacoes/<int:recomendacao_id>/aplicar", methods=["POST"])
@login_required
def recomendacao_aplicar(recomendacao_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        ok = agent_service.atualizar_status_recomendacao(
            recomendacao_id,
            "aplicada",
            current_app,
            detalhe="Aplicada manualmente pelo painel admin",
        )
        if ok:
            flash("Recomendação marcada como aplicada.", "success")
        else:
            flash("Recomendação não encontrada ou status inválido.", "warning")
    except Exception as e:
        flash(f"Erro ao atualizar recomendação: {str(e)}", "danger")
    return redirect(url_for("admin.admin_dashboard"))


@admin_bp.route("/recomendacoes/<int:recomendacao_id>/descartar", methods=["POST"])
@login_required
def recomendacao_descartar(recomendacao_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        ok = agent_service.atualizar_status_recomendacao(
            recomendacao_id,
            "descartada",
            current_app,
            detalhe="Descartada manualmente pelo painel admin",
        )
        if ok:
            flash("Recomendação descartada.", "success")
        else:
            flash("Recomendação não encontrada ou status inválido.", "warning")
    except Exception as e:
        flash(f"Erro ao descartar recomendação: {str(e)}", "danger")
    return redirect(url_for("admin.admin_dashboard"))


# --- Planos e termos ---
@admin_bp.route("/planos")
@login_required
def gestao_planos():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    config_atual = plano_service.obter_config_planos()
    active_term = get_active_term()
    active_privacy_policy = get_active_privacy_policy()
    return render_template(
        "planos.html",
        config=config_atual,
        planos_saas=config_atual["planos_saas"],
        active_term=active_term,
        active_privacy_policy=active_privacy_policy,
        freemium_trial_dias=config_atual["freemium_trial_dias"],
    )


@admin_bp.route("/controle-usuarios", methods=["GET"])
@login_required
def controle_usuarios():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return render_template("controle_usuarios.html")


@admin_bp.route("/controle-usuarios/convidar-adm", methods=["POST"])
@login_required
def controle_usuarios_convidar_adm():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    email = (request.form.get("email_convite_admin") or "").strip().lower()
    user = user_admin_control_service.buscar_usuario_por_email(email)
    if not user:
        flash("Usuário não encontrado para o e-mail informado.", "warning")
        return redirect(url_for("admin.controle_usuarios"))
    if user.is_admin:
        flash("Este usuário já é administrador.", "info")
        return redirect(url_for("admin.controle_usuarios"))
    token = user_admin_control_service.gerar_token_admin_action(
        secret_key=current_app.config["SECRET_KEY"],
        action=user_admin_control_service.ADMIN_ACTION_PROMOTE,
        target_user_id=user.id,
        requested_by_user_id=current_user.id,
    )
    confirm_url = url_for(
        "admin_promocao_confirmar",
        token=token,
        _external=True,
    )
    try:
        user_admin_control_service.enviar_email_convite_admin(
            target_user=user,
            confirm_url=confirm_url,
        )
        flash(
            f"Convite de administrador enviado para {user.email}.",
            "success",
        )
    except Exception as e:
        _log.exception("Erro ao enviar convite de admin para %s: %s", user.email, e)
        flash("Não foi possível enviar o convite de administrador.", "danger")
    return redirect(url_for("admin.controle_usuarios"))


@admin_bp.route("/controle-usuarios/revogar-adm", methods=["POST"])
@login_required
def controle_usuarios_revogar_adm():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    email = (request.form.get("email_revogar_admin") or "").strip().lower()
    user = user_admin_control_service.buscar_usuario_por_email(email)
    if not user:
        flash("Usuário não encontrado para o e-mail informado.", "warning")
        return redirect(url_for("admin.controle_usuarios"))
    if not user.is_admin:
        flash("Este usuário já não é administrador.", "info")
        return redirect(url_for("admin.controle_usuarios"))
    if current_user.id == user.id:
        flash("Não é permitido solicitar auto-revogação de administrador.", "warning")
        return redirect(url_for("admin.controle_usuarios"))
    if user_admin_control_service.total_admins_ativos() <= 1:
        flash("Não é permitido revogar o último administrador ativo.", "warning")
        return redirect(url_for("admin.controle_usuarios"))
    token = user_admin_control_service.gerar_token_admin_action(
        secret_key=current_app.config["SECRET_KEY"],
        action=user_admin_control_service.ADMIN_ACTION_REVOKE,
        target_user_id=user.id,
        requested_by_user_id=current_user.id,
    )
    confirm_url = url_for(
        "admin_revogacao_confirmar",
        token=token,
        _external=True,
    )
    try:
        user_admin_control_service.enviar_email_revogacao_admin(
            target_user=user,
            confirm_url=confirm_url,
        )
        flash(
            "Solicitação de revogação enviada para diogo@agentefrete.com.br.",
            "success",
        )
    except Exception as e:
        _log.exception(
            "Erro ao enviar e-mail de revogação de admin para aprovação: %s",
            e,
        )
        flash("Não foi possível enviar o e-mail de confirmação de revogação.", "danger")
    return redirect(url_for("admin.controle_usuarios"))


@admin_bp.route("/controle-usuarios/atribuir-plano", methods=["POST"])
@login_required
def controle_usuarios_atribuir_plano():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    email = (request.form.get("email_plano_usuario") or "").strip()
    plano = (request.form.get("plano_usuario") or "").strip()
    qtd_franquias = (
        request.form.get("multiuser_qtd_franquias") or ""
    ).strip()
    try:
        resultado = user_plan_control_service.atribuir_plano_para_usuario(
            email=email,
            plano_raw=plano,
            quantidade_franquias_raw=qtd_franquias or None,
            admin_user_id=current_user.id,
        )
        msg = (
            f"Plano de {resultado.user_email} alterado de "
            f"{resultado.plano_anterior or 'free'} para {resultado.plano_novo}."
        )
        if resultado.plano_novo == user_plan_control_service.PLANO_MULTIUSER:
            if resultado.franquias_multiuser_criadas > 0:
                msg += (
                    f" Franquias criadas: {resultado.franquias_multiuser_criadas}."
                )
            if resultado.codigos_gerados:
                msg += " Códigos gerados: " + ", ".join(resultado.codigos_gerados) + "."
        flash(msg, "success")
    except ValueError as e:
        flash(str(e), "warning")
    except Exception as e:
        _log.exception("Erro ao atribuir plano para usuário %s: %s", email, e)
        flash("Não foi possível atribuir o plano solicitado.", "danger")
    return redirect(url_for("admin.controle_usuarios"))


@admin_bp.route("/planos/termos/upload", methods=["POST"])
@login_required
def planos_termos_upload():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    if "termo_pdf" not in request.files:
        flash("Selecione um arquivo PDF para enviar.", "danger")
        return redirect(url_for("admin.gestao_planos"))
    file = request.files["termo_pdf"]
    if not file or not file.filename:
        flash("Nenhum arquivo selecionado.", "warning")
        return redirect(url_for("admin.gestao_planos"))
    if not termo_service.extensao_termo_permitida(file.filename):
        flash(
            "Apenas arquivos .pdf são permitidos para os Termos de Uso.",
            "danger",
        )
        return redirect(url_for("admin.gestao_planos"))
    try:
        sent, failed = termo_service.processar_upload_termo(current_app, file)
        flash(
            f"Termo de Uso atualizado com sucesso. Notificações enviadas: {sent}."
            + (f" Falhas: {failed}." if failed else ""),
            "success",
        )
    except Exception as e:
        _log.exception("Erro ao fazer upload do termo de uso: %s", e)
        flash(f"Erro ao enviar termo de uso: {str(e)}", "danger")
    return redirect(url_for("admin.gestao_planos"))


@admin_bp.route("/planos/privacy-policy/upload", methods=["POST"])
@login_required
def planos_privacy_policy_upload():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    if "privacy_policy_pdf" not in request.files:
        flash("Selecione um arquivo PDF para enviar.", "danger")
        return redirect(url_for("admin.gestao_planos"))
    file = request.files["privacy_policy_pdf"]
    if not file or not file.filename:
        flash("Nenhum arquivo selecionado.", "warning")
        return redirect(url_for("admin.gestao_planos"))
    if not privacy_policy_service.extensao_privacy_policy_permitida(file.filename):
        flash(
            "Apenas arquivos .pdf são permitidos para a Política de Privacidade.",
            "danger",
        )
        return redirect(url_for("admin.gestao_planos"))
    try:
        _, sent, failed, notification_mode = privacy_policy_service.processar_upload_privacy_policy(
            current_app,
            file,
            uploaded_by_user_id=getattr(current_user, "id", None),
        )
        if notification_mode == "async":
            mensagem = (
                "Política de Privacidade atualizada com sucesso. "
                "Notificações operacionais agendadas em background."
            )
        else:
            mensagem = (
                "Política de Privacidade atualizada com sucesso. "
                f"Notificações enviadas: {sent}."
            )
            if failed:
                mensagem += f" Falhas: {failed}."
        flash(mensagem, "success")
    except Exception as e:
        _log.exception("Erro ao fazer upload da Política de Privacidade: %s", e)
        flash(f"Erro ao enviar Política de Privacidade: {str(e)}", "danger")
    return redirect(url_for("admin.gestao_planos"))


@admin_bp.route("/planos/saas/salvar", methods=["POST"])
@login_required
def planos_saas_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    plano_codigo = (request.form.get("plano_codigo") or "").strip()
    valor_plano = (request.form.get("valor_plano") or "").strip()
    franquia_limite_total = (request.form.get("franquia_limite_total") or "").strip()
    freemium_trial_dias = (request.form.get("freemium_trial_dias") or "").strip()
    gateway_provider = (request.form.get("gateway_provider") or "").strip()
    gateway_product_id = (request.form.get("gateway_product_id") or "").strip()
    gateway_price_id = (request.form.get("gateway_price_id") or "").strip()
    gateway_currency = (request.form.get("gateway_currency") or "").strip()
    gateway_interval = (request.form.get("gateway_interval") or "").strip()
    gateway_pronto = bool(request.form.get("gateway_pronto"))
    try:
        resultado = plano_service.atualizar_parametros_plano_admin(
            plano_codigo=plano_codigo,
            valor_plano_raw=valor_plano,
            franquia_limite_total_raw=franquia_limite_total,
            gateway_provider_raw=gateway_provider or None,
            gateway_product_id_raw=gateway_product_id or None,
            gateway_price_id_raw=gateway_price_id or None,
            gateway_currency_raw=gateway_currency or None,
            gateway_interval_raw=gateway_interval or None,
            gateway_pronto_raw=gateway_pronto,
        )
        trial_salvo = None
        if plano_codigo.lower() == "free" and freemium_trial_dias:
            trial_salvo = plano_service.salvar_freemium_trial_dias(
                freemium_trial_dias
            )
        gateway_info = resultado.get("gateway_config") or {}
        gateway_situacao = ""
        if gateway_info:
            if gateway_info.get("configuracao_valida"):
                gateway_situacao = " Gateway externo: configurado e pronto."
            else:
                pendencias_gateway = ", ".join(gateway_info.get("pendencias", []))
                gateway_situacao = (
                    " Gateway externo com pendencias: "
                    + (pendencias_gateway or "configuracao_incompleta")
                    + "."
                )
        flash(
            (
                f"Plano {resultado['plano_nome']} salvo com valor R$ {resultado['valor_plano']} "
                f"e franquia "
                f"{resultado['franquia_limite_total']} créditos. "
                + (
                    (
                        "Trial atualizado para ilimitado. "
                        if trial_salvo is not None and trial_salvo >= 999999999
                        else f"Trial atualizado para {trial_salvo} dias. "
                    )
                    if trial_salvo is not None
                    else ""
                )
                + f"Franquias atualizadas: {resultado['franquias_atualizadas']}/{resultado['franquias_total']}."
                + gateway_situacao
            ),
            "success",
        )
    except ValueError as e:
        flash(str(e), "warning")
    except Exception as e:
        _log.exception("Erro ao atualizar parâmetros do plano via admin: %s", e)
        flash("Erro ao salvar parâmetros do plano. Tente novamente.", "danger")
    return redirect(url_for("admin.gestao_planos"))


# --- Importacao ---
@admin_bp.route("/importacao")
@login_required
def importacao_dados():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    execucoes_indices = agent_service.ler_execucoes_indices_admin()
    return render_template(
        "importacao.html",
        execucoes_indices=execucoes_indices,
        finance_frequencia_minutos=obter_finance_frequencia_minutos(),
        finance_frequencia_horas=obter_finance_frequencia_horas(),
        finance_frequencia_label=_formatar_frequencia_minutos(
            obter_finance_frequencia_minutos()
        ),
        frequencia_minima_admin=_min_frequencia_minutos_para_admin(),
    )


@admin_bp.route("/indices/frequencia", methods=["POST"])
@login_required
def indices_configurar_frequencia():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    valor_raw = (request.form.get("finance_frequencia_minutos") or "").strip()
    try:
        valor = _validar_frequencia_minutos_admin(
            valor_raw,
            contexto="a atualizacao financeira",
        )
    except ValueError as e:
        flash(f"Valor de frequencia financeira invalido. {str(e)}", "warning")
        return redirect(url_for("admin.importacao_dados"))
    try:
        configurar_finance_frequencia_minutos(valor)
        flash(
            f"Frequencia automatica dos indices atualizada para {_formatar_frequencia_minutos(valor)}.",
            "success",
        )
    except Exception as e:
        flash(f"Erro ao atualizar frequencia financeira: {str(e)}", "danger")
    return redirect(url_for("admin.importacao_dados"))
    try:
        valor = int(valor_raw)
        if valor < 1:
            raise ValueError("Frequencia deve ser maior que zero.")
    except (ValueError, TypeError):
        flash(
            "Valor de frequencia financeira invalido. Informe horas inteiras (ex.: 1, 3, 6, 12).",
            "warning",
        )
        return redirect(url_for("admin.importacao_dados"))
    try:
        configurar_finance_frequencia_horas(valor)
        flash(f"Frequencia automatica dos indices atualizada para {valor}h.", "success")
    except Exception as e:
        flash(f"Erro ao atualizar frequencia financeira: {str(e)}", "danger")
    return redirect(url_for("admin.importacao_dados"))


@admin_bp.route("/processar_importacao/<tipo>", methods=["POST"])
@login_required
def executar_importacao(tipo):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    if "arquivo" not in request.files:
        flash("Selecione um arquivo para continuar.", "danger")
        return redirect(url_for("admin.importacao_dados"))
    file = request.files["arquivo"]
    if not file or file.filename == "":
        flash("Arquivo inválido.", "warning")
        return redirect(url_for("admin.importacao_dados"))
    filename = secure_filename(file.filename)
    if tipo == "operacao":
        if not (filename.endswith(".txt") or filename.endswith(".csv")):
            flash(
                "Erro: Envie um arquivo .txt ou .csv seguindo o padrão de colunas.",
                "danger",
            )
            return redirect(url_for("admin.importacao_dados"))
        sucessos, falhas, _path_suc, _path_erro = (
            import_service.processar_importacao_operacao(file)
        )
        flash(
            f"Importação concluída! Sucessos: {sucessos} | Bloqueados/Erros: {falhas}. Logs em /logs",
            "success",
        )
        return redirect(url_for("admin.importacao_dados"))
    if tipo == "tabelas":
        if not file.filename.strip().lower().endswith(".txt"):
            flash("Por favor, envie um arquivo .txt válido.", "danger")
            return redirect(url_for("admin.importacao_dados"))
        sucessos, linhas_com_erro, erro_critico = (
            import_service.processar_importacao_tabelas(file)
        )
        if erro_critico:
            flash(f"Erro crítico: {erro_critico}", "danger")
            return redirect(url_for("admin.importacao_dados"))
        if linhas_com_erro:
            relatorio_txt = (
                "LOG DE ERROS - IMPORTAÇÃO\n" + "=" * 30 + "\n"
            )
            relatorio_txt += "\n".join(linhas_com_erro)
            output = io.BytesIO()
            output.write(relatorio_txt.encode("utf-8"))
            output.seek(0)
            flash(
                f"Processamento parcial: {sucessos} linhas importadas. Verifique o log de erros.",
                "warning",
            )
            return send_file(
                output,
                mimetype="text/plain",
                as_attachment=True,
                download_name=f"erros_importacao_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            )
        flash(f"Sucesso total! {sucessos} linhas importadas.", "success")
        return redirect(url_for("admin.importacao_dados"))
    flash("Tipo de importação não suportado.", "warning")
    return redirect(url_for("admin.importacao_dados"))


@admin_bp.route("/indices/atualizar-manual", methods=["POST"])
@login_required
def indices_atualizar_manual():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        resultado = atualizar_indices(bypass_frequencia=True) or {}
        agent_service.persistir_execucao_indices_admin(resultado)
        status_global = resultado.get("status_global") or "desconhecido"
        mensagem = resultado.get("mensagem") or "Execução manual dos índices concluída."
        if status_global == "sucesso":
            flash(mensagem, "success")
        elif status_global == "sucesso_parcial":
            flash(mensagem, "warning")
        else:
            flash(mensagem, "danger")
    except Exception as e:
        _log.exception(
            "Falha ao executar atualização manual de índices via admin: %s", e
        )
        flash(f"Erro ao atualizar índices financeiros: {str(e)}", "danger")
    return redirect(url_for("admin.importacao_dados"))


# --- Séries editoriais ---
@admin_bp.route("/series", methods=["GET"])
@login_required
def series_editoriais():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    editar_id = request.args.get("editar_id", type=int)
    series = serie_service.listar_series()
    serie_edicao = serie_service.obter_serie_por_id(editar_id) if editar_id else None
    return render_template(
        "series_editoriais.html",
        series=series,
        serie_edicao=serie_edicao,
    )


@admin_bp.route("/series/salvar", methods=["POST"])
@login_required
def series_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    serie_id = request.form.get("id", type=int)
    nome = (request.form.get("nome") or "").strip()
    tema = (request.form.get("tema") or "").strip()
    objetivo_lead = (request.form.get("objetivo_lead") or "").strip()
    cta_base = (request.form.get("cta_base") or "").strip()
    descricao = (request.form.get("descricao") or "").strip()
    cadencia_dias = request.form.get("cadencia_dias", type=int) or 1
    ativo = bool(request.form.get("ativo"))
    serie, err = serie_service.salvar_serie(
        serie_id, nome, tema, objetivo_lead, cta_base, descricao, cadencia_dias, ativo
    )
    if err:
        flash(err, "warning" if "não encontrada" in err else "danger")
    else:
        flash("Série salva com sucesso.", "success")
    return redirect(url_for("admin.series_editoriais"))


@admin_bp.route("/series/<int:serie_id>/toggle", methods=["POST"])
@login_required
def series_toggle(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    ok, err = serie_service.toggle_serie_ativo(serie_id)
    if err:
        flash(err, "warning")
    else:
        flash("Status da série atualizado.", "success")
    return redirect(url_for("admin.series_editoriais"))


@admin_bp.route("/series/<int:serie_id>/itens", methods=["GET"])
@login_required
def series_itens(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    serie = serie_service.obter_serie_por_id(serie_id)
    if not serie:
        return "Série não encontrada", 404
    editar_id = request.args.get("editar_id", type=int)
    itens = serie_service.listar_itens_serie(serie_id)
    item_edicao = (
        serie_service.obter_item_serie(serie_id, editar_id) if editar_id else None
    )
    return render_template(
        "series_itens.html",
        serie=serie,
        itens=itens,
        item_edicao=item_edicao,
    )


@admin_bp.route("/series/<int:serie_id>/itens/salvar", methods=["POST"])
@login_required
def series_itens_salvar(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    item_id = request.form.get("id", type=int)
    ordem = request.form.get("ordem", type=int) or 1
    titulo_planejado = (request.form.get("titulo_planejado") or "").strip()
    subtitulo_planejado = (
        (request.form.get("subtitulo_planejado") or "").strip()
    )
    data_str = (request.form.get("data_planejada") or "").strip()
    status = (request.form.get("status") or "planejado").strip()
    data_planejada = None
    if data_str:
        try:
            data_planejada = datetime.strptime(data_str, "%Y-%m-%d")
        except ValueError:
            flash("Data planejada inválida.", "warning")
            return redirect(url_for("admin.series_itens", serie_id=serie_id))
    item, err = serie_service.salvar_item_serie(
        serie_id,
        item_id,
        ordem,
        titulo_planejado,
        subtitulo_planejado,
        data_planejada,
        status,
    )
    if err:
        flash(err, "warning")
    else:
        flash("Item salvo com sucesso.", "success")
    return redirect(url_for("admin.series_itens", serie_id=serie_id))


@admin_bp.route(
    "/series/<int:serie_id>/itens/<int:item_id>/reabrir", methods=["POST"]
)
@login_required
def series_itens_reabrir(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Informe um motivo para reabrir o item.", "warning")
        return redirect(url_for("admin.series_itens", serie_id=serie_id))
    ok, err = serie_service.reabrir_item(
        getattr(current_user, "email", None), serie_id, item_id, motivo
    )
    if err and "inválida" in err:
        flash(err, "warning")
    elif err:
        flash(err, "danger")
    else:
        flash("Item reaberto como planejado.", "success")
    return redirect(url_for("admin.series_itens", serie_id=serie_id))


@admin_bp.route(
    "/series/<int:serie_id>/itens/<int:item_id>/pular", methods=["POST"]
)
@login_required
def series_itens_pular(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Informe um motivo para pular o item.", "warning")
        return redirect(url_for("admin.series_itens", serie_id=serie_id))
    ok, err = serie_service.pular_item(
        getattr(current_user, "email", None), serie_id, item_id, motivo
    )
    if err and "inválida" in err:
        flash(err, "warning")
    elif err:
        flash(err, "danger")
    else:
        flash("Item marcado como pulado.", "success")
    return redirect(url_for("admin.series_itens", serie_id=serie_id))


@admin_bp.route(
    "/series/<int:serie_id>/itens/<int:item_id>/vincular-pauta",
    methods=["POST"],
)
@login_required
def series_itens_vincular_pauta(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    pauta_id = request.form.get("pauta_id", type=int)
    motivo = (request.form.get("motivo") or "").strip()
    if not pauta_id:
        flash("Informe o ID da pauta a vincular.", "warning")
        return redirect(url_for("admin.series_itens", serie_id=serie_id))
    if not motivo:
        flash("Informe um motivo para o vínculo.", "warning")
        return redirect(url_for("admin.series_itens", serie_id=serie_id))
    ok, msg = serie_service.vincular_pauta_item(
        getattr(current_user, "email", None),
        serie_id,
        item_id,
        pauta_id,
        motivo,
    )
    if msg == "idempotente":
        flash("Pauta já estava vinculada a este item.", "info")
    elif msg:
        flash(msg, "warning")
    else:
        flash("Pauta vinculada ao item com sucesso.", "success")
    return redirect(url_for("admin.series_itens", serie_id=serie_id))


@admin_bp.route(
    "/series/<int:serie_id>/itens/<int:item_id>/desvincular-pauta",
    methods=["POST"],
)
@login_required
def series_itens_desvincular_pauta(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Informe um motivo para o desvínculo.", "warning")
        return redirect(url_for("admin.series_itens", serie_id=serie_id))
    ok, msg = serie_service.desvincular_pauta_item(
        getattr(current_user, "email", None), serie_id, item_id, motivo
    )
    if msg == "idempotente":
        flash("Item já está sem pauta vinculada.", "info")
    elif msg:
        flash(msg, "warning")
    else:
        flash("Pauta desvinculada do item com sucesso.", "success")
    return redirect(url_for("admin.series_itens", serie_id=serie_id))


# --- Pautas ---
@admin_bp.route("/pautas", methods=["GET"])
@login_required
def pautas_admin():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    tipo = (request.args.get("tipo") or "").strip()
    status = (request.args.get("status") or "").strip()
    status_verificacao = (request.args.get("status_verificacao") or "").strip()
    data_ini = (request.args.get("data_ini") or "").strip()
    data_fim = (request.args.get("data_fim") or "").strip()
    serie_id = request.args.get("serie_id", type=int)
    editar_id = request.args.get("editar_id", type=int)
    pautas, status_verificacao_permitidos_list = pauta_service.listar_pautas(
        tipo=tipo or None,
        status=status or None,
        status_verificacao=status_verificacao or None,
        data_ini=data_ini or None,
        data_fim=data_fim or None,
        serie_id=serie_id,
    )
    pauta_edicao = None
    if editar_id:
        from app.models import Pauta
        pauta_edicao = Pauta.query.filter_by(id=editar_id).first()
    return render_template(
        "pautas.html",
        pautas=pautas,
        pauta_edicao=pauta_edicao,
        status_verificacao_permitidos=status_verificacao_permitidos_list,
    )


@admin_bp.route("/pautas/salvar", methods=["POST"])
@login_required
def pautas_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    pauta_id = request.form.get("id", type=int)
    titulo_original = (request.form.get("titulo_original") or "").strip()
    link = (request.form.get("link") or "").strip()
    fonte = (request.form.get("fonte") or "").strip()
    tipo = (request.form.get("tipo") or "artigo").strip()
    status = (request.form.get("status") or "pendente").strip()
    status_ver = (request.form.get("status_verificacao") or "").strip()
    motivo_admin = (request.form.get("motivo_admin") or "").strip()
    pauta, err = pauta_service.salvar_pauta(
        getattr(current_user, "email", None),
        pauta_id,
        titulo_original,
        link,
        fonte,
        tipo,
        status,
        status_ver,
        motivo_admin or None,
    )
    if err:
        flash(err, "warning" if "obrigatório" in err or "não encontrada" in err else "danger")
    else:
        flash("Pauta salva com sucesso.", "success")
    return redirect(url_for("admin.pautas_admin"))


@admin_bp.route("/pautas/<int:pauta_id>/arquivar", methods=["POST"])
@login_required
def pautas_arquivar(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    ok, err = pauta_service.arquivar_pauta(
        getattr(current_user, "email", None), pauta_id, motivo or None
    )
    if err:
        flash(err, "warning")
    else:
        flash(
            "Pauta arquivada. Ela não será mais utilizada automaticamente pelo Cleiton.",
            "success",
        )
    return redirect(url_for("admin.pautas_admin"))


@admin_bp.route("/pautas/<int:pauta_id>/reprocessar", methods=["POST"])
@login_required
def pautas_reprocessar(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    ok, err = pauta_service.reprocessar_pauta(
        getattr(current_user, "email", None), pauta_id, motivo or None
    )
    if err:
        flash(err, "warning")
    else:
        flash("Pauta marcada para reprocessamento (status pendente).", "success")
    return redirect(url_for("admin.pautas_admin"))


@admin_bp.route("/pautas/<int:pauta_id>/marcar-revisao", methods=["POST"])
@login_required
def pautas_marcar_revisao(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get("motivo") or "").strip()
    ok, err = pauta_service.marcar_revisao_pauta(
        getattr(current_user, "email", None), pauta_id, motivo or None
    )
    if err:
        flash(err, "warning")
    else:
        flash("Pauta marcada para revisão manual.", "success")
    return redirect(url_for("admin.pautas_admin"))
