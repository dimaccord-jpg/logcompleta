"""
Rotas do painel administrativo.
Apenas definição de rotas, autenticação, autorização e renderização de templates.
Toda lógica de negócio está em app.services e app.tasks.
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
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
import os
import io
import logging
import threading
from concurrent.futures import Future
from datetime import datetime

from app.infra import get_admin_executor
from app.terms_services import get_active_term
from app.finance import atualizar_indices
from app.run_julia_regras import status_verificacao_permitidos

from app.services import agent_service
from app.services import pauta_service
from app.services import serie_service
from app.services import import_service
from app.services import plano_service
from app.services import termo_service
from app.services import auditoria_service

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

# Estado para execução async (Cleiton / artigo manual)
_CLEITON_FUTURE: Future | None = None
_ARTIGO_MANUAL_FUTURE: Future | None = None
_CLEITON_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


def verificar_acesso_admin():
    """Retorna True se o usuário estiver autenticado e for administrador."""
    return current_user.is_authenticated and getattr(
        current_user, "is_admin", False
    )


# --- Dashboard ---
@admin_bp.route("/")
@admin_bp.route("/dashboard")
@login_required
def admin_dashboard():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    status_sistema = {
        "api_indices": "Online",
        "db_localidades": "Conectado",
        "status_geral": "Operacional",
        "mensagem_sistema": "Painel Cleiton Log Ativo",
    }
    kpis_insight = agent_service.obter_kpis_insight()
    recomendacoes_recentes = agent_service.obter_recomendacoes_recentes(
        limite=15
    )
    return render_template(
        "dashboard.html",
        status=status_sistema,
        kpis_insight=kpis_insight,
        recomendacoes_recentes=recomendacoes_recentes,
    )


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
    frequencia_horas = agent_service.obter_frequencia_horas()
    ultima_execucao, proxima_prevista = (
        agent_service.obter_ultima_e_proxima_execucao(frequencia_horas)
    )
    janela_inicio, janela_fim = agent_service.obter_janela_publicacao()
    status_pautas_artigo = agent_service.obter_status_pautas_artigo()
    ultima_artigo = agent_service.obter_ultima_publicacao_artigo()
    ultima_execucao_manual = agent_service.ler_ultima_execucao_manual()
    return render_template(
        "agentes_julia.html",
        frequencia_horas=frequencia_horas,
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
    valor_raw = (request.form.get("frequencia_horas") or "").strip()
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


@admin_bp.route("/agentes/roberto")
@login_required
def agentes_roberto():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return render_template("agentes_roberto.html")


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
            _CLEITON_FUTURE = executor.submit(
                agent_tasks.run_cleiton_background,
                app_obj,
                bypass_frequencia,
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
            _ARTIGO_MANUAL_FUTURE = executor.submit(
                agent_tasks.run_artigo_manual_background,
                app_obj,
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
    return render_template(
        "planos.html",
        config=config_atual,
        active_term=active_term,
        julia_chat_max_history=config_atual["julia_chat_max_history"],
        freemium_consultas_dia=config_atual["freemium_consultas_dia"],
        freemium_trial_dias=config_atual["freemium_trial_dias"],
    )


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


@admin_bp.route("/planos/freemium/salvar", methods=["POST"])
@login_required
def planos_freemium_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    raw_history = request.form.get("julia_chat_max_history", "").strip()
    raw_consultas = request.form.get("freemium_consultas_dia", "").strip()
    raw_trial = request.form.get("freemium_trial_dias", "").strip()
    try:
        msgs = plano_service.salvar_limite_freemium(
            julia_chat_max_history_raw=raw_history or None,
            freemium_consultas_dia_raw=raw_consultas or None,
            freemium_trial_dias_raw=raw_trial or None,
        )
        if msgs:
            flash("Configuração freemium salva: " + ", ".join(msgs), "success")
        else:
            flash(
                "Nenhum valor válido enviado. Preencha ao menos um campo.",
                "warning",
            )
    except Exception as e:
        flash("Erro ao salvar configuração. Tente novamente.", "danger")
    return redirect(url_for("admin.gestao_planos"))


@admin_bp.route("/planos/atualizar", methods=["POST"])
@login_required
def atualizar_precos():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    plano = request.form.get("plano_tipo")
    valor = request.form.get("valor")
    flash(
        f"Simulação: Plano {plano} seria atualizado para R$ {valor}.",
        "info",
    )
    return redirect(url_for("admin.gestao_planos"))


# --- Importação ---
@admin_bp.route("/importacao")
@login_required
def importacao_dados():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    execucoes_indices = agent_service.ler_execucoes_indices_admin()
    return render_template(
        "importacao.html", execucoes_indices=execucoes_indices
    )


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
        resultado = atualizar_indices() or {}
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
