from flask import Blueprint, render_template, flash, redirect, url_for, request, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
from sqlalchemy import text
import os
import csv
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from app.extensions import db
from app.models import (
    FreteReal,
    RecomendacaoEstrategica,
    InsightCanal,
    AuditoriaGerencial,
    ConfigRegras,
    Pauta,
    NoticiaPortal,
    SerieEditorial,
    SerieItemEditorial,
)
from app.run_julia_regras import status_verificacao_permitidos
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

base_dir = os.path.dirname(os.path.abspath(__file__))
pasta_templates = os.path.join(base_dir, 'template_admin')

admin_bp = Blueprint('admin', __name__, 
                     template_folder=pasta_templates, 
                     url_prefix='/admin')

_CLEITON_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleiton-admin")
_CLEITON_FUTURE: Future | None = None
_CLEITON_LOCK = threading.Lock()


def _admin_exec_mode() -> str:
    """
    Define modo de execução do botão admin:
    - homolog/prod: async (evita timeout de worker)
    - dev: sync (mantém feedback detalhado para testes locais)
    Pode ser sobrescrito por ADMIN_CLEITON_EXEC_MODE=sync|async.
    """
    forced = (os.getenv("ADMIN_CLEITON_EXEC_MODE", "") or "").strip().lower()
    if forced in ("sync", "async"):
        return forced
    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    return "async" if app_env in ("homolog", "prod") else "sync"


def _executar_cleiton_em_background(app_obj, bypass_frequencia: bool) -> None:
    """Executa ciclo completo no background e registra resultado no log."""
    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(app_obj, bypass_frequencia=bypass_frequencia) or {}
        logging.info(
            "Cleiton admin (async) concluído: status=%s mission_id=%s motivo=%s",
            resultado.get("status"),
            resultado.get("mission_id"),
            resultado.get("motivo"),
        )
    except Exception as e:
        logging.exception("Falha no ciclo Cleiton admin (async): %s", e)

# --- FUNÇÃO DE APOIO (BACKOFFICE SEGURANÇA) ---
def verificar_acesso_admin():
    """Retorna True se o usuário estiver autenticado e for administrador."""
    return current_user.is_authenticated and getattr(current_user, 'is_admin', False)


def _registrar_auditoria_admin(
    tipo_decisao: str,
    decisao: str,
    entidade: str,
    entidade_id: int | None,
    estado_antes: dict | None,
    estado_depois: dict | None,
    motivo: str | None,
    resultado: str,
    detalhe: str | None = None,
) -> None:
    """
    Helper padronizado para trilha de auditoria administrativa.
    tipo_decisao: admin_operacao | admin_vinculo | admin_reprocessamento etc.
    resultado: sucesso | falha | ignorado
    """
    try:
        contexto = {
            "ator": getattr(current_user, "email", None),
            "entidade": entidade,
            "entidade_id": entidade_id,
            "antes": estado_antes,
            "depois": estado_depois,
            "motivo": motivo,
        }
        auditoria_registrar(
            tipo_decisao=tipo_decisao,
            decisao=decisao,
            contexto=contexto,
            resultado=resultado,
            detalhe=detalhe,
        )
    except Exception:
        # Auditoria não deve quebrar fluxo do admin.
        logging.exception("Falha ao registrar auditoria admin para %s id=%s", entidade, entidade_id)

# --- ROTA 1: DASHBOARD (Fase 6: KPIs insight + recomendações) ---
@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def admin_dashboard():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    status_sistema = {
        "api_indices": "Online",
        "db_localidades": "Conectado",
        "status_geral": "Operacional",
        "mensagem_sistema": "Painel Cleiton Log Ativo"
    }
    # KPIs estratégicos (Fase 6): insight e recomendações
    kpis_insight = _obter_kpis_insight()
    recomendacoes_recentes = _obter_recomendacoes_recentes(limite=15)
    return render_template(
        'dashboard.html',
        status=status_sistema,
        kpis_insight=kpis_insight,
        recomendacoes_recentes=recomendacoes_recentes,
    )


def _obter_kpis_insight():
    """Retorna dict com contagens para painel estratégico (dentro de app_context)."""
    try:
        pendentes = RecomendacaoEstrategica.query.filter_by(status="pendente").count()
        aplicadas = RecomendacaoEstrategica.query.filter_by(status="aplicada").count()
        descartadas = RecomendacaoEstrategica.query.filter_by(status="descartada").count()
        total_metricas = InsightCanal.query.count()
        total_auditorias_insight = AuditoriaGerencial.query.filter_by(tipo_decisao="insight").count()
        return {
            "recomendacoes_pendentes": pendentes,
            "recomendacoes_aplicadas": aplicadas,
            "recomendacoes_descartadas": descartadas,
            "total_metricas": total_metricas,
            "total_auditorias_insight": total_auditorias_insight,
        }
    except Exception:
        return {
            "recomendacoes_pendentes": 0,
            "recomendacoes_aplicadas": 0,
            "recomendacoes_descartadas": 0,
            "total_metricas": 0,
            "total_auditorias_insight": 0,
        }


def _obter_recomendacoes_recentes(limite=15):
    """Lista recomendações recentes (todas as status) para exibição no dashboard."""
    try:
        return (
            RecomendacaoEstrategica.query
            .order_by(RecomendacaoEstrategica.criado_em.desc())
            .limit(max(1, min(50, limite)))
            .all()
        )
    except Exception:
        return []


# --- ROTA 1.1: AGENTES (Júlia / Roberto) ---
@admin_bp.route('/agentes')
@login_required
def agentes_home():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return redirect(url_for('admin.agentes_julia'))


@admin_bp.route('/agentes/julia')
@login_required
def agentes_julia():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    frequencia_horas = _obter_frequencia_horas()
    ultima_execucao, proxima_prevista = _obter_ultima_e_proxima_execucao(frequencia_horas)
    janela_inicio, janela_fim = _obter_janela_publicacao()
    status_pautas_artigo = _obter_status_pautas_artigo()
    ultima_artigo = _obter_ultima_publicacao_artigo()
    return render_template(
        'agentes_julia.html',
        frequencia_horas=frequencia_horas,
        ultima_execucao=ultima_execucao,
        proxima_prevista=proxima_prevista,
        janela_inicio=janela_inicio,
        janela_fim=janela_fim,
        status_pautas_artigo=status_pautas_artigo,
        ultima_artigo=ultima_artigo,
    )


def _obter_frequencia_horas() -> int:
    """Retorna a frequência atual do ciclo (fallback seguro = 3h)."""
    try:
        from app.run_cleiton_agente_regras import bootstrap_regras, CHAVE_FREQUENCIA_HORAS, DEFAULTS
        bootstrap_regras()
        cfg = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
        if cfg and cfg.valor_inteiro is not None:
            return max(1, int(cfg.valor_inteiro))
        return int(DEFAULTS.get(CHAVE_FREQUENCIA_HORAS, 3))
    except Exception:
        return 3


def _obter_ultima_e_proxima_execucao(frequencia_horas: int) -> tuple[datetime | None, datetime | None]:
    """
    Retorna (última execução de orquestração sem bypass, próxima execução prevista).
    Próxima = última + frequência em horas. Usado apenas para exibição; o ciclo real
    depende de haver processo/cron rodando (ex.: run_cleiton.py em loop ou job agendado).
    """
    try:
        from app.run_cleiton_agente_orquestrador import ultima_auditoria_orquestracao
        from datetime import timedelta
        ultima = ultima_auditoria_orquestracao()
        if ultima is None:
            return None, None
        proxima = ultima + timedelta(hours=max(1, frequencia_horas))
        return ultima, proxima
    except Exception:
        return None, None


def _obter_janela_publicacao() -> tuple[int, int]:
    """Retorna (hora_inicio, hora_fim) da janela de publicação (0-23)."""
    try:
        from app.run_cleiton_agente_regras import get_janela_publicacao
        return get_janela_publicacao()
    except Exception:
        return 6, 22


def _obter_status_pautas_artigo() -> dict:
    """Resumo de backlog de pautas de artigo para o painel admin."""
    try:
        status_permitidos = status_verificacao_permitidos()
        total = Pauta.query.filter(Pauta.tipo == "artigo", Pauta.arquivada.isnot(True)).count()
        pendentes = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "pendente",
            Pauta.arquivada.isnot(True),
        ).count()
        elegiveis = (
            Pauta.query.filter(
                Pauta.tipo == "artigo",
                Pauta.status == "pendente",
                Pauta.status_verificacao.in_(status_permitidos),
                Pauta.arquivada.isnot(True),
            ).count()
        )
        em_proc = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "em_processamento",
            Pauta.arquivada.isnot(True),
        ).count()
        falha = Pauta.query.filter(
            Pauta.tipo == "artigo",
            Pauta.status == "falha",
            Pauta.arquivada.isnot(True),
        ).count()
        return {
            "total": total,
            "pendentes": pendentes,
            "elegiveis": elegiveis,
            "em_processamento": em_proc,
            "falha": falha,
        }
    except Exception:
        return {
            "total": 0,
            "pendentes": 0,
            "elegiveis": 0,
            "em_processamento": 0,
            "falha": 0,
        }


def _obter_ultima_publicacao_artigo():
    """Retorna a última data de publicação efetiva de artigo no portal (ou None)."""
    try:
        ultimo = (
            NoticiaPortal.query.filter(
                NoticiaPortal.tipo == "artigo",
                NoticiaPortal.status_publicacao.in_(["publicado", "parcial"]),
            )
            .order_by(NoticiaPortal.publicado_em.desc(), NoticiaPortal.data_publicacao.desc())
            .first()
        )
        if not ultimo:
            return None
        return ultimo.publicado_em or ultimo.data_publicacao
    except Exception:
        return None


@admin_bp.route('/agentes/julia/frequencia', methods=['POST'])
@login_required
def agentes_julia_configurar_frequencia():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    valor_raw = (request.form.get('frequencia_horas') or '').strip()
    try:
        valor = int(valor_raw)
        if valor < 1:
            raise ValueError('Frequência deve ser maior que zero.')
    except Exception:
        flash('Valor de frequência inválido. Informe horas inteiras (ex.: 1, 3, 6).', 'warning')
        return redirect(url_for('admin.agentes_julia'))

    try:
        from app.run_cleiton_agente_regras import bootstrap_regras, CHAVE_FREQUENCIA_HORAS
        bootstrap_regras()
        cfg = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_HORAS).first()
        if not cfg:
            cfg = ConfigRegras(chave=CHAVE_FREQUENCIA_HORAS, descricao='Intervalo de execução do ciclo em horas')
            db.session.add(cfg)
        cfg.valor_inteiro = valor
        cfg.valor_texto = None
        cfg.valor_real = None
        db.session.commit()
        flash(f'Frequência do ciclo atualizada para {valor}h.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao atualizar frequência: {str(e)}', 'danger')
    return redirect(url_for('admin.agentes_julia'))


@admin_bp.route('/agentes/roberto')
@login_required
def agentes_roberto():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return render_template('agentes_roberto.html')


@admin_bp.route('/agentes/julia/executar-cleiton', methods=['POST'])
@login_required
def agentes_julia_executar_cleiton():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    bypass_frequencia = (request.form.get('bypass_frequencia') or '').strip() in ('1', 'true', 'True')

    if _admin_exec_mode() == "async":
        global _CLEITON_FUTURE
        with _CLEITON_LOCK:
            if _CLEITON_FUTURE and not _CLEITON_FUTURE.done():
                flash("Já existe uma execução do Cleiton em andamento. Aguarde a conclusão.", "warning")
                return redirect(url_for('admin.agentes_julia'))
            app_obj = current_app._get_current_object()
            _CLEITON_FUTURE = _CLEITON_EXECUTOR.submit(
                _executar_cleiton_em_background,
                app_obj,
                bypass_frequencia,
            )
        flash(
            "Execução do Cleiton iniciada em segundo plano. Acompanhe os logs para status final.",
            "info",
        )
        return redirect(url_for('admin.agentes_julia'))

    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(current_app, bypass_frequencia=bypass_frequencia) or {}
        status = resultado.get("status") or "falha"
        motivo = resultado.get("motivo") or "Ciclo não informou motivo detalhado."
        mission_id = resultado.get("mission_id")
        tipo_missao = resultado.get("tipo_missao")
        scout = resultado.get("scout") or {}
        verif = resultado.get("verificador") or {}

        partes_msg = [motivo]
        if mission_id:
            partes_msg.append(f"mission_id={mission_id}")
        if tipo_missao:
            partes_msg.append(f"tipo_missao={tipo_missao}")
        if scout:
            partes_msg.append(
                f"Scout: inseridas={scout.get('inseridas', 0)}, "
                f"reativadas={scout.get('reativadas', 0)}, "
                f"ignoradas={scout.get('ignoradas_duplicata', 0)}, "
                f"erros={scout.get('erros', 0)}"
            )
            if "fontes_processadas" in scout:
                partes_msg.append(
                    f"Fontes Scout: processadas={scout.get('fontes_processadas', 0)}, "
                    f"com_erro={scout.get('fontes_com_erro', 0)}, "
                    f"sem_itens={scout.get('fontes_sem_itens', 0)}"
                )
        if verif:
            partes_msg.append(
                f"Verificador: aprovadas={verif.get('aprovadas', 0)}, "
                f"revisar={verif.get('revisar', 0)}, "
                f"rejeitadas={verif.get('rejeitadas', 0)}"
            )
        mensagem = " | ".join(partes_msg)

        if status == "sucesso":
            flash(mensagem, "success")
        elif status == "ignorado":
            flash(mensagem, "warning")
        else:
            flash(mensagem, "danger")
    except Exception as e:
        logging.exception("Falha ao executar Cleiton via admin (bypass_frequencia=%s): %s", bypass_frequencia, e)
        flash(f"Erro ao executar Cleiton: {str(e)}", "danger")
    return redirect(url_for('admin.agentes_julia'))


@admin_bp.route('/agentes/julia/executar-artigo-manual', methods=['POST'])
@login_required
def agentes_julia_executar_artigo_manual():
    """Dispara manualmente uma missão de artigo, ignorando apenas a trava diária de artigo publicado hoje."""
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(
            current_app,
            bypass_frequencia=True,
            tipo_missao_forcado="artigo",
            ignorar_trava_artigo_hoje=True,
        ) or {}
        status = resultado.get("status") or "falha"
        motivo = resultado.get("motivo_final") or resultado.get("motivo") or "Execução manual de artigo concluída sem motivo detalhado."
        caminho = resultado.get("caminho_usado")
        partes_msg: list[str] = [motivo]
        if caminho:
            partes_msg.append(f"caminho_usado={caminho}")
        if resultado.get("mission_id"):
            partes_msg.append(f"mission_id={resultado.get('mission_id')}")

        msg_texto = " | ".join(partes_msg)
        link_em_proc = url_for('admin.pautas_admin', tipo='artigo', status='em_processamento')
        link_publicadas = url_for('admin.pautas_admin', tipo='artigo', status='publicada')
        mensagem = Markup(
            f"{escape(msg_texto)} | "
            f"<a href=\"{escape(link_em_proc)}\">Acompanhar em processamento</a> | "
            f"<a href=\"{escape(link_publicadas)}\">Acompanhar publicadas</a>"
        )
        if status == "sucesso":
            flash(mensagem, "success")
        elif status == "ignorado":
            flash(mensagem, "warning")
        else:
            flash(mensagem, "danger")
    except Exception as e:
        logging.exception("Falha ao executar artigo manual via admin: %s", e)
        flash(f"Erro ao executar artigo manual: {str(e)}", "danger")
    return redirect(url_for('admin.agentes_julia'))
@admin_bp.route('/agentes/julia/coletar-noticias', methods=['POST'])
@login_required
def agentes_julia_coletar_noticias():
    """Ação opcional: executa apenas Scout + Verificador para notícias automáticas."""
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        from app.run_cleiton_agente_scout import executar_coleta
        from app.run_cleiton_agente_verificador import executar_verificacao
        resultado_scout = executar_coleta()
        resultado_verif = executar_verificacao()
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
    return redirect(url_for('admin.agentes_julia'))


# --- Fase 6: Gestão de recomendações (aplicar/descartar) ---
@admin_bp.route('/recomendacoes/<int:recomendacao_id>/aplicar', methods=['POST'])
@login_required
def recomendacao_aplicar(recomendacao_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao
        ok = atualizar_status_recomendacao(
            recomendacao_id, "aplicada", current_app,
            detalhe="Aplicada manualmente pelo painel admin",
        )
        if ok:
            flash("Recomendação marcada como aplicada.", "success")
        else:
            flash("Recomendação não encontrada ou status inválido.", "warning")
    except Exception as e:
        flash(f"Erro ao atualizar recomendação: {str(e)}", "danger")
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/recomendacoes/<int:recomendacao_id>/descartar', methods=['POST'])
@login_required
def recomendacao_descartar(recomendacao_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        from app.run_cleiton_agente_customer_insight import atualizar_status_recomendacao
        ok = atualizar_status_recomendacao(
            recomendacao_id, "descartada", current_app,
            detalhe="Descartada manualmente pelo painel admin",
        )
        if ok:
            flash("Recomendação descartada.", "success")
        else:
            flash("Recomendação não encontrada ou status inválido.", "warning")
    except Exception as e:
        flash(f"Erro ao descartar recomendação: {str(e)}", "danger")
    return redirect(url_for('admin.admin_dashboard'))

# --- ROTA 2: GESTÃO DE PLANOS ---
@admin_bp.route('/planos')
@login_required
def gestao_planos():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    
    # Mantendo seus dados de exemplo para preencher o esqueleto da tela
    config_atual = {
        "plano_ativo": "Premium",
        "indice_reajuste": 1.05
    }
    return render_template('planos.html', config=config_atual)

# --- ROTA DE APOIO: ATUALIZAR PREÇOS (O que resolve o erro 500) ---
@admin_bp.route('/planos/atualizar', methods=['POST'])
@login_required
def atualizar_precos():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    
    # Como ainda não temos banco de dados, apenas simulamos o recebimento
    plano = request.form.get('plano_tipo')
    valor = request.form.get('valor')
    
    flash(f"Simulação: Plano {plano} seria atualizado para R$ {valor}.", "info")
    
    return redirect(url_for('admin.gestao_planos'))

# --- ROTA 3: IMPORTAÇÃO DE DADOS ---
@admin_bp.route('/importacao')
@login_required
def importacao_dados():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    return render_template('importacao.html')

# --- ROTAS DE AÇÃO (POST) ---

@admin_bp.route('/processar_importacao/<tipo>', methods=['POST'])
@login_required
def executar_importacao(tipo):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403

    if 'arquivo' not in request.files:
        flash("Selecione um arquivo para continuar.", "danger")
        return redirect(url_for('admin.importacao_dados'))

    file = request.files['arquivo']
    if file.filename == '':
        flash("Arquivo inválido.", "warning")
        return redirect(url_for('admin.importacao_dados'))

    if file:
        filename = secure_filename(file.filename)
        
        # --- LÓGICA PARA CARGA DE OPERAÇÃO (ARQUIVO .TXT / .CSV) ---
        if tipo == 'operacao':
            if not (filename.endswith('.txt') or filename.endswith('.csv')):
                flash("Erro: Envie um arquivo .txt ou .csv seguindo o padrão de colunas.", "danger")
                return redirect(url_for('admin.importacao_dados'))
            
            try:
                # 1. Preparar pasta e arquivos de LOG
                # Busca o diretório de logs do .env ou usa um fallback local
                log_dir_base = os.getenv('LOG_DIR', os.path.join(os.getcwd(), 'logs_fallback'))
                if not os.path.exists(log_dir_base): 
                    os.makedirs(log_dir_base, exist_ok=True)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                path_sucesso = os.path.join(log_dir_base, f"sucesso_{timestamp}.txt")
                path_erro = os.path.join(log_dir_base, f"erro_{timestamp}.txt")

                # 2. Ler o conteúdo do arquivo
                conteudo = file.read().decode('utf-8-sig')
                stream = io.StringIO(conteudo)
                leitor = csv.DictReader(stream, delimiter=',') # Padrão vírgula conforme seu teste_upload.txt

                sucessos, falhas = 0, 0
                engine_localidades = db.engines['localidades']
                
                with engine_localidades.connect() as connection:
                    for linha in leitor:
                        # Extração dos dados baseada nos rótulos do seu arquivo .txt
                        uf_nome = linha.get('uf_nome')
                        cidade_nome = linha.get('cidade_nome')
                        chave_origem = linha.get('chave_busca')
                        id_uf = linha.get('id_uf')
                        id_cidade = linha.get('id_cidade')

                        if not chave_origem: continue

                        # REGRA 6: Chave sempre em minúscula
                        chave_proc = str(chave_origem).strip().lower()

                        # REGRA 4: Verificar se a chave já existe (Mestre)
                        query_check = text("SELECT 1 FROM de_para_logistica WHERE chave_busca = :ch")
                        existe = connection.execute(query_check, {"ch": chave_proc}).fetchone()

                        if existe:
                            with open(path_erro, "a", encoding="utf-8") as f:
                                f.write(f"BLOQUEADO: Chave [{chave_proc}] já existe no banco.\n")
                            falhas += 1
                            continue

                        # REGRA 5: Inserção segura (Trata caracteres especiais como D'Oeste)
                        try:
                            query_ins = text("""
                                INSERT INTO de_para_logistica (uf_nome, cidade_nome, chave_busca, id_uf, id_cidade)
                                VALUES (:uf, :cid, :ch, :i_uf, :i_cid)
                            """)
                            connection.execute(query_ins, {
                                "uf": uf_nome, "cid": cidade_nome, "ch": chave_proc,
                                "i_uf": id_uf, "i_cid": id_cidade
                            })
                            connection.commit()
                            
                            with open(path_sucesso, "a", encoding="utf-8") as f:
                                f.write(f"SUCESSO: {chave_proc} cadastrada.\n")
                            sucessos += 1
                        except Exception as e_row:
                            with open(path_erro, "a", encoding="utf-8") as f:
                                f.write(f"ERRO TÉCNICO [{chave_proc}]: {str(e_row)}\n")
                            falhas += 1

                flash(f"Importação concluída! Sucessos: {sucessos} | Bloqueados/Erros: {falhas}. Logs em /logs", "success")
                
            except Exception as e:
                flash(f"Erro crítico ao processar o arquivo: {str(e)}", "danger")

# --- LÓGICA PARA TABELAS DE FRETE (.TXT) ---
        elif tipo == 'tabelas':
            if not file or not file.filename.endswith('.txt'):
                flash("Por favor, envie um arquivo .txt válido.", "danger")
                return redirect(url_for('admin.importacao_dados'))

            try:
                file.seek(0)
                conteudo = file.read().decode("utf-8")
                stream = io.StringIO(conteudo)
                leitor = csv.DictReader(stream)
                
                sucessos = 0
                linhas_com_erro = [] # Lista para armazenar o log de falhas

                engine_loc = db.engines['localidades']

                for i, linha in enumerate(leitor, start=1):
                    try:
                        cid_orig = linha.get('cidade_origem', '').strip()
                        uf_orig = linha.get('uf_origem', '').strip()
                        cid_dest = linha.get('cidade_destino', '').strip()
                        uf_dest = linha.get('uf_destino', '').strip()

                        # Chaves de busca
                        chave_orig = f"{cid_orig.lower()}-{uf_orig.lower()}"
                        chave_dest = f"{cid_dest.lower()}-{uf_dest.lower()}"

                        with engine_loc.connect() as conn:
                            res_orig = conn.execute(text("SELECT id_cidade FROM de_para_logistica WHERE chave_busca = :c"), {'c': chave_orig}).fetchone()
                            res_dest = conn.execute(text("SELECT id_cidade FROM de_para_logistica WHERE chave_busca = :c"), {'c': chave_dest}).fetchone()

                        # --- VALIDAÇÃO DE INTEGRIDADE (SEM ABORTAR O PROCESSO) ---
                        if not res_orig or not res_dest:
                            falha = chave_orig if not res_orig else chave_dest
                            linhas_com_erro.append(f"Linha {i}: Localidade '{falha}' não encontrada.")
                            continue

                        # --- TRATAMENTO DE DATA (Transforma string em objeto Date) ---
                        data_str = linha.get('data_emissao', '').strip()
                        data_obj = None
                        if data_str:
                            try:
                                # Ajuste o formato '%d/%m/%Y' conforme o seu .txt (ex: 24/02/2026)
                                data_obj = datetime.strptime(data_str, '%d/%m/%Y').date()
                            except:
                                linhas_com_erro.append(f"Linha {i}: Formato de data inválido ({data_str}).")
                                continue

                        # Criação do registro
                        novo_frete = FreteReal(
                            data_emissao=data_obj, # Agora salva como DATE
                            id_cidade_origem=res_orig[0],
                            id_cidade_destino=res_dest[0],
                            cidade_origem=cid_orig,
                            uf_origem=uf_orig,
                            cidade_destino=cid_dest,
                            uf_destino=uf_dest,
                            peso_real=float(linha.get('peso_real') or 0),
                            valor_nf=float(linha.get('valor_nf') or 0),
                            valor_frete_total=float(linha.get('valor_frete_total') or 0),
                            valor_imposto=float(linha['valor_imposto']) if linha.get('valor_imposto') else None,
                            modal=linha.get('modal', '').lower()
                        )
                        db.session.add(novo_frete)
                        sucessos += 1

                    except Exception as e_row:
                        linhas_com_erro.append(f"Linha {i}: Erro inesperado - {str(e_row)}")

                db.session.commit()

                # --- GERENCIAMENTO DE RESPOSTA ---
                if linhas_com_erro:
                    # Gera um arquivo de log para download automático
                    relatorio_txt = "LOG DE ERROS - IMPORTAÇÃO\n" + "="*30 + "\n"
                    relatorio_txt += "\n".join(linhas_com_erro)
                    
                    output = io.BytesIO()
                    output.write(relatorio_txt.encode('utf-8'))
                    output.seek(0)
                    
                    flash(f"Processamento parcial: {sucessos} linhas importadas. Verifique o log de erros.", "warning")
                    
                    from flask import send_file
                    return send_file(
                        output,
                        mimetype="text/plain",
                        as_attachment=True,
                        download_name=f"erros_importacao_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
                    )

                flash(f"Sucesso total! {sucessos} linhas importadas.", "success")
                return redirect(url_for('admin.importacao_dados'))

            except Exception as e:
                db.session.rollback()
                flash(f"Erro crítico: {str(e)}", "danger")
                return redirect(url_for('admin.importacao_dados'))


# --- ROTA 4: SÉRIES EDITORIAIS ---
@admin_bp.route('/series', methods=['GET'])
@login_required
def series_editoriais():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    editar_id = request.args.get('editar_id', type=int)
    serie_edicao = None
    if editar_id:
        serie_edicao = SerieEditorial.query.filter_by(id=editar_id).first()
    series = (
        SerieEditorial.query.order_by(SerieEditorial.ativo.desc(), SerieEditorial.created_at.desc()).all()
    )
    return render_template(
        'series_editoriais.html',
        series=series,
        serie_edicao=serie_edicao,
    )


@admin_bp.route('/series/salvar', methods=['POST'])
@login_required
def series_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    serie_id = request.form.get('id', type=int)
    nome = (request.form.get('nome') or '').strip()
    tema = (request.form.get('tema') or '').strip()
    objetivo_lead = (request.form.get('objetivo_lead') or '').strip()
    cta_base = (request.form.get('cta_base') or '').strip()
    descricao = (request.form.get('descricao') or '').strip()
    cadencia_dias = request.form.get('cadencia_dias', type=int) or 1
    ativo = bool(request.form.get('ativo'))
    if not nome or not tema:
        flash('Nome e tema são obrigatórios.', 'warning')
        return redirect(url_for('admin.series_editoriais'))
    try:
        if serie_id:
            serie = SerieEditorial.query.filter_by(id=serie_id).first()
            if not serie:
                flash('Série não encontrada.', 'warning')
                return redirect(url_for('admin.series_editoriais'))
        else:
            serie = SerieEditorial()
            db.session.add(serie)
        serie.nome = nome
        serie.tema = tema
        serie.objetivo_lead = objetivo_lead or None
        serie.cta_base = cta_base or None
        serie.descricao = descricao or None
        serie.cadencia_dias = max(1, cadencia_dias)
        serie.ativo = ativo
        db.session.commit()
        flash('Série salva com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao salvar série: {str(e)}', 'danger')
    return redirect(url_for('admin.series_editoriais'))


@admin_bp.route('/series/<int:serie_id>/toggle', methods=['POST'])
@login_required
def series_toggle(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    try:
        serie = SerieEditorial.query.filter_by(id=serie_id).first()
        if not serie:
            flash('Série não encontrada.', 'warning')
            return redirect(url_for('admin.series_editoriais'))
        serie.ativo = not bool(serie.ativo)
        db.session.commit()
        flash('Status da série atualizado.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao atualizar série: {str(e)}', 'danger')
    return redirect(url_for('admin.series_editoriais'))


@admin_bp.route('/series/<int:serie_id>/itens', methods=['GET'])
@login_required
def series_itens(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    serie = SerieEditorial.query.filter_by(id=serie_id).first()
    if not serie:
        return "Série não encontrada", 404
    editar_id = request.args.get('editar_id', type=int)
    item_edicao = None
    if editar_id:
        item_edicao = SerieItemEditorial.query.filter_by(id=editar_id, serie_id=serie.id).first()
    itens = (
        SerieItemEditorial.query.filter_by(serie_id=serie.id)
        .order_by(SerieItemEditorial.data_planejada.asc(), SerieItemEditorial.ordem.asc(), SerieItemEditorial.id.asc())
        .all()
    )
    return render_template(
        'series_itens.html',
        serie=serie,
        itens=itens,
        item_edicao=item_edicao,
    )


@admin_bp.route('/series/<int:serie_id>/itens/salvar', methods=['POST'])
@login_required
def series_itens_salvar(serie_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    item_id = request.form.get('id', type=int)
    ordem = request.form.get('ordem', type=int) or 1
    titulo_planejado = (request.form.get('titulo_planejado') or '').strip()
    subtitulo_planejado = (request.form.get('subtitulo_planejado') or '').strip()
    data_str = (request.form.get('data_planejada') or '').strip()
    status = (request.form.get('status') or 'planejado').strip()
    if status not in ['planejado', 'em_andamento', 'publicado', 'falha', 'pulado']:
        status = 'planejado'
    data_planejada = None
    if data_str:
        try:
            data_planejada = datetime.strptime(data_str, '%Y-%m-%d')
        except Exception:
            flash('Data planejada inválida.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
    try:
        serie = SerieEditorial.query.filter_by(id=serie_id).first()
        if not serie:
            flash('Série não encontrada.', 'warning')
            return redirect(url_for('admin.series_editoriais'))
        if item_id:
            item = SerieItemEditorial.query.filter_by(id=item_id, serie_id=serie.id).first()
            if not item:
                flash('Item não encontrado.', 'warning')
                return redirect(url_for('admin.series_itens', serie_id=serie_id))
        else:
            item = SerieItemEditorial(serie_id=serie.id)
            db.session.add(item)
        item.ordem = max(1, ordem)
        item.titulo_planejado = titulo_planejado or None
        item.subtitulo_planejado = subtitulo_planejado or None
        item.data_planejada = data_planejada
        item.status = status
        db.session.commit()
        flash('Item salvo com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao salvar item: {str(e)}', 'danger')
    return redirect(url_for('admin.series_itens', serie_id=serie_id))


@admin_bp.route('/series/<int:serie_id>/itens/<int:item_id>/reabrir', methods=['POST'])
@login_required
def series_itens_reabrir(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get('motivo') or '').strip()
    if not motivo:
        flash('Informe um motivo para reabrir o item.', 'warning')
        return redirect(url_for('admin.series_itens', serie_id=serie_id))
    try:
        item = SerieItemEditorial.query.filter_by(id=item_id, serie_id=serie_id).first()
        if not item:
            flash('Item não encontrado.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        estado_antes = {"status": item.status, "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None}
        from app.run_cleiton_agente_serie import atualizar_status_item
        ok = atualizar_status_item(item.id, 'planejado', motivo=motivo)
        if ok:
            db.session.refresh(item)
            estado_depois = {"status": item.status, "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None}
            _registrar_auditoria_admin(
                tipo_decisao="admin_operacao",
                decisao="Reabrir item de série para planejado",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes=estado_antes,
                estado_depois=estado_depois,
                motivo=motivo,
                resultado="sucesso",
            )
            flash('Item reaberto como planejado.', 'success')
        else:
            _registrar_auditoria_admin(
                tipo_decisao="admin_operacao",
                decisao="Reabertura de item de série bloqueada pela máquina de estados",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes=estado_antes,
                estado_depois=None,
                motivo=motivo,
                resultado="ignorado",
            )
            flash('Transição de status inválida para o item selecionado.', 'warning')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Erro ao reabrir item de série",
            entidade="serie_item",
            entidade_id=item_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao reabrir item: {str(e)}', 'danger')
    return redirect(url_for('admin.series_itens', serie_id=serie_id))


@admin_bp.route('/series/<int:serie_id>/itens/<int:item_id>/pular', methods=['POST'])
@login_required
def series_itens_pular(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    motivo = (request.form.get('motivo') or '').strip()
    if not motivo:
        flash('Informe um motivo para pular o item.', 'warning')
        return redirect(url_for('admin.series_itens', serie_id=serie_id))
    try:
        item = SerieItemEditorial.query.filter_by(id=item_id, serie_id=serie_id).first()
        if not item:
            flash('Item não encontrado.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        estado_antes = {"status": item.status, "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None}
        from app.run_cleiton_agente_serie import atualizar_status_item
        ok = atualizar_status_item(item.id, 'pulado', motivo=motivo)
        if ok:
            db.session.refresh(item)
            estado_depois = {"status": item.status, "data_planejada": item.data_planejada.isoformat() if item.data_planejada else None}
            _registrar_auditoria_admin(
                tipo_decisao="admin_operacao",
                decisao="Pular item de série",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes=estado_antes,
                estado_depois=estado_depois,
                motivo=motivo,
                resultado="sucesso",
            )
            flash('Item marcado como pulado.', 'success')
        else:
            _registrar_auditoria_admin(
                tipo_decisao="admin_operacao",
                decisao="Marcação de item como pulado bloqueada pela máquina de estados",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes=estado_antes,
                estado_depois=None,
                motivo=motivo,
                resultado="ignorado",
            )
            flash('Transição de status inválida para o item selecionado.', 'warning')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Erro ao pular item de série",
            entidade="serie_item",
            entidade_id=item_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao pular item: {str(e)}', 'danger')
    return redirect(url_for('admin.series_itens', serie_id=serie_id))


@admin_bp.route('/series/<int:serie_id>/itens/<int:item_id>/vincular-pauta', methods=['POST'])
@login_required
def series_itens_vincular_pauta(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import SerieItemEditorial, Pauta

    pauta_id = request.form.get('pauta_id', type=int)
    motivo = (request.form.get('motivo') or '').strip()
    if not pauta_id:
        flash('Informe o ID da pauta a vincular.', 'warning')
        return redirect(url_for('admin.series_itens', serie_id=serie_id))
    if not motivo:
        flash('Informe um motivo para o vínculo.', 'warning')
        return redirect(url_for('admin.series_itens', serie_id=serie_id))
    try:
        item = SerieItemEditorial.query.filter_by(id=item_id, serie_id=serie_id).first()
        if not item:
            flash('Item não encontrado.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            flash('Pauta não encontrada.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        if pauta.tipo != "artigo":
            flash('Somente pautas do tipo artigo podem ser vinculadas a itens de série.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        if getattr(pauta, "arquivada", False):
            flash('Pauta arquivada não pode ser vinculada.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        if item.status == "publicado":
            flash('Item em estado publicado não pode receber novo vínculo de pauta.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        # Pauta já vinculada a outro item
        from app.models import SerieItemEditorial as SerieItemModel

        existente = SerieItemModel.query.filter(
            SerieItemModel.pauta_id == pauta.id,
            SerieItemModel.id != item.id,
        ).first()
        if existente:
            _registrar_auditoria_admin(
                tipo_decisao="admin_vinculo",
                decisao="Tentativa de vínculo de pauta já vinculada a outro item",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes={"pauta_id": item.pauta_id},
                estado_depois=None,
                motivo=motivo,
                resultado="ignorado",
            )
            flash('Pauta já está vinculada a outro item de série.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        estado_antes = {"pauta_id": item.pauta_id}
        if item.pauta_id == pauta.id:
            # Idempotente: nada a mudar, mas registra auditoria amigável.
            _registrar_auditoria_admin(
                tipo_decisao="admin_vinculo",
                decisao="Vínculo de pauta já existente (idempotente)",
                entidade="serie_item",
                entidade_id=item.id,
                estado_antes=estado_antes,
                estado_depois=estado_antes,
                motivo=motivo,
                resultado="sucesso",
            )
            flash('Pauta já estava vinculada a este item.', 'info')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        item.pauta_id = pauta.id
        db.session.commit()
        estado_depois = {"pauta_id": item.pauta_id}
        _registrar_auditoria_admin(
            tipo_decisao="admin_vinculo",
            decisao="Vincular pauta a item de série",
            entidade="serie_item",
            entidade_id=item.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo,
            resultado="sucesso",
        )
        flash('Pauta vinculada ao item com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_vinculo",
            decisao="Erro ao vincular pauta a item de série",
            entidade="serie_item",
            entidade_id=item_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao vincular pauta ao item: {str(e)}', 'danger')
    return redirect(url_for('admin.series_itens', serie_id=serie_id))


@admin_bp.route('/series/<int:serie_id>/itens/<int:item_id>/desvincular-pauta', methods=['POST'])
@login_required
def series_itens_desvincular_pauta(serie_id, item_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import SerieItemEditorial

    motivo = (request.form.get('motivo') or '').strip()
    if not motivo:
        flash('Informe um motivo para o desvínculo.', 'warning')
        return redirect(url_for('admin.series_itens', serie_id=serie_id))
    try:
        item = SerieItemEditorial.query.filter_by(id=item_id, serie_id=serie_id).first()
        if not item:
            flash('Item não encontrado.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        if not item.pauta_id:
            flash('Item já está sem pauta vinculada.', 'info')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        if item.status == "publicado":
            flash('Item publicado não pode ter vínculo de pauta removido.', 'warning')
            return redirect(url_for('admin.series_itens', serie_id=serie_id))
        estado_antes = {"pauta_id": item.pauta_id}
        item.pauta_id = None
        db.session.commit()
        estado_depois = {"pauta_id": item.pauta_id}
        _registrar_auditoria_admin(
            tipo_decisao="admin_vinculo",
            decisao="Desvincular pauta de item de série",
            entidade="serie_item",
            entidade_id=item.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo,
            resultado="sucesso",
        )
        flash('Pauta desvinculada do item com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_vinculo",
            decisao="Erro ao desvincular pauta de item de série",
            entidade="serie_item",
            entidade_id=item_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao desvincular pauta do item: {str(e)}', 'danger')
    return redirect(url_for('admin.series_itens', serie_id=serie_id))


# --- ROTA 5: CRUD DE PAUTAS ---
@admin_bp.route('/pautas', methods=['GET'])
@login_required
def pautas_admin():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import Pauta, SerieItemEditorial

    tipo = (request.args.get('tipo') or '').strip()
    status = (request.args.get('status') or '').strip()
    status_verificacao = (request.args.get('status_verificacao') or '').strip()
    data_ini = (request.args.get('data_ini') or '').strip()
    data_fim = (request.args.get('data_fim') or '').strip()
    serie_id = request.args.get('serie_id', type=int)
    editar_id = request.args.get('editar_id', type=int)

    query = Pauta.query
    if tipo:
        query = query.filter(Pauta.tipo == tipo)
    if status:
        query = query.filter(Pauta.status == status)
    if status_verificacao:
        query = query.filter(Pauta.status_verificacao == status_verificacao)
    if data_ini:
        try:
            dt_ini = datetime.strptime(data_ini, '%Y-%m-%d')
            query = query.filter(Pauta.created_at >= dt_ini)
        except Exception:
            pass
    if data_fim:
        try:
            dt_fim = datetime.strptime(data_fim, '%Y-%m-%d')
            dt_fim = dt_fim.replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(Pauta.created_at <= dt_fim)
        except Exception:
            pass
    if serie_id:
        query = query.join(SerieItemEditorial, SerieItemEditorial.pauta_id == Pauta.id).filter(
            SerieItemEditorial.serie_id == serie_id
        )

    pautas_raw = query.order_by(Pauta.created_at.desc()).limit(200).all()
    # Mapa de vínculos pauta -> item de série (se existir)
    ids_pauta = [p.id for p in pautas_raw]
    vinculos = {}
    if ids_pauta:
        itens = SerieItemEditorial.query.filter(SerieItemEditorial.pauta_id.in_(ids_pauta)).all()
        for it in itens:
            vinculos[it.pauta_id] = it
    pautas = [(p, vinculos.get(p.id)) for p in pautas_raw]

    pauta_edicao = None
    if editar_id:
        pauta_edicao = Pauta.query.filter_by(id=editar_id).first()

    return render_template(
        'pautas.html',
        pautas=pautas,
        pauta_edicao=pauta_edicao,
        status_verificacao_permitidos=status_verificacao_permitidos(),
    )


@admin_bp.route('/pautas/salvar', methods=['POST'])
@login_required
def pautas_salvar():
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import Pauta

    pauta_id = request.form.get('id', type=int)
    titulo_original = (request.form.get('titulo_original') or '').strip()
    link = (request.form.get('link') or '').strip()
    fonte = (request.form.get('fonte') or '').strip()
    tipo = (request.form.get('tipo') or 'artigo').strip()
    status = (request.form.get('status') or 'pendente').strip()
    status_ver = (request.form.get('status_verificacao') or '').strip()
    motivo_admin = (request.form.get('motivo_admin') or '').strip()

    if not titulo_original or not link:
        flash('Título e link são obrigatórios para a pauta.', 'warning')
        return redirect(url_for('admin.pautas_admin'))

    tipos_validos = {'noticia', 'artigo'}
    if tipo not in tipos_validos:
        tipo = 'artigo'
    status_validos = {'pendente', 'em_processamento', 'publicada', 'falha'}
    if status not in status_validos:
        status = 'pendente'
    status_permitidos = set(status_verificacao_permitidos() + ['revisar', 'rejeitado'])
    if status_ver not in status_permitidos:
        status_ver = status_verificacao_permitidos()[0]

    try:
        if pauta_id:
            pauta = Pauta.query.filter_by(id=pauta_id).first()
            if not pauta:
                flash('Pauta não encontrada.', 'warning')
                return redirect(url_for('admin.pautas_admin'))
        else:
            pauta = Pauta()
            db.session.add(pauta)
        estado_antes = None
        if pauta.id:
            estado_antes = {
                "titulo_original": pauta.titulo_original,
                "link": pauta.link,
                "tipo": pauta.tipo,
                "status": pauta.status,
                "status_verificacao": pauta.status_verificacao,
                "fonte": pauta.fonte,
                "arquivada": bool(pauta.arquivada),
            }
        pauta.titulo_original = titulo_original[:500]
        pauta.link = link[:500]
        pauta.fonte = fonte[:200] or None
        pauta.tipo = tipo
        pauta.status = status
        pauta.status_verificacao = status_ver
        # Pautas criadas/alteradas via admin devem manter fonte manual para rastreabilidade.
        pauta.fonte_tipo = "manual"
        db.session.commit()
        estado_depois = {
            "titulo_original": pauta.titulo_original,
            "link": pauta.link,
            "tipo": pauta.tipo,
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "fonte": pauta.fonte,
            "arquivada": bool(pauta.arquivada),
        }
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Criar/editar pauta via admin",
            entidade="pauta",
            entidade_id=pauta.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo_admin or None,
            resultado="sucesso",
        )
        flash('Pauta salva com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Erro ao salvar pauta via admin",
            entidade="pauta",
            entidade_id=pauta_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo_admin or None,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao salvar pauta: {str(e)}', 'danger')
    return redirect(url_for('admin.pautas_admin'))


@admin_bp.route('/pautas/<int:pauta_id>/arquivar', methods=['POST'])
@login_required
def pautas_arquivar(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import Pauta

    motivo = (request.form.get('motivo') or '').strip()
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            flash('Pauta não encontrada.', 'warning')
            return redirect(url_for('admin.pautas_admin'))
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "arquivada": bool(pauta.arquivada),
        }
        pauta.arquivada = True
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
            "arquivada": bool(pauta.arquivada),
        }
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Arquivar pauta manualmente",
            entidade="pauta",
            entidade_id=pauta.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo or None,
            resultado="sucesso",
        )
        flash('Pauta arquivada. Ela não será mais utilizada automaticamente pelo Cleiton.', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Erro ao arquivar pauta",
            entidade="pauta",
            entidade_id=pauta_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo or None,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao arquivar pauta: {str(e)}', 'danger')
    return redirect(url_for('admin.pautas_admin'))


@admin_bp.route('/pautas/<int:pauta_id>/reprocessar', methods=['POST'])
@login_required
def pautas_reprocessar(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import Pauta

    motivo = (request.form.get('motivo') or '').strip()
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            flash('Pauta não encontrada.', 'warning')
            return redirect(url_for('admin.pautas_admin'))
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        pauta.status = "pendente"
        # Mantém status_verificacao atual; admin pode combiná-lo com "revisar" se necessário.
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        _registrar_auditoria_admin(
            tipo_decisao="admin_reprocessamento",
            decisao="Reprocessar pauta manualmente",
            entidade="pauta",
            entidade_id=pauta.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo or None,
            resultado="sucesso",
        )
        flash('Pauta marcada para reprocessamento (status pendente).', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_reprocessamento",
            decisao="Erro ao reprocessar pauta",
            entidade="pauta",
            entidade_id=pauta_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo or None,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao reprocessar pauta: {str(e)}', 'danger')
    return redirect(url_for('admin.pautas_admin'))


@admin_bp.route('/pautas/<int:pauta_id>/marcar-revisao', methods=['POST'])
@login_required
def pautas_marcar_revisao(pauta_id):
    if not verificar_acesso_admin():
        return "Acesso Negado", 403
    from app.models import Pauta

    motivo = (request.form.get('motivo') or '').strip()
    try:
        pauta = Pauta.query.filter_by(id=pauta_id).first()
        if not pauta:
            flash('Pauta não encontrada.', 'warning')
            return redirect(url_for('admin.pautas_admin'))
        estado_antes = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        pauta.status_verificacao = "revisar"
        db.session.commit()
        estado_depois = {
            "status": pauta.status,
            "status_verificacao": pauta.status_verificacao,
        }
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Marcar pauta para revisão manual",
            entidade="pauta",
            entidade_id=pauta.id,
            estado_antes=estado_antes,
            estado_depois=estado_depois,
            motivo=motivo or None,
            resultado="sucesso",
        )
        flash('Pauta marcada para revisão manual.', 'success')
    except Exception as e:
        db.session.rollback()
        _registrar_auditoria_admin(
            tipo_decisao="admin_operacao",
            decisao="Erro ao marcar pauta para revisão",
            entidade="pauta",
            entidade_id=pauta_id,
            estado_antes=None,
            estado_depois=None,
            motivo=motivo or None,
            resultado="falha",
            detalhe=str(e),
        )
        flash(f'Erro ao marcar pauta para revisão: {str(e)}', 'danger')
    return redirect(url_for('admin.pautas_admin'))