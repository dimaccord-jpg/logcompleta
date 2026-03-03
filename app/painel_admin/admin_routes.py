from flask import Blueprint, render_template, flash, redirect, url_for, request
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import text # Importante para rodar o SQL puro
import os
import csv
import io
from datetime import datetime
from extensions import db # Criado para atender extensions
from models import FreteReal # <--- Adicione esta linha

base_dir = os.path.dirname(os.path.abspath(__file__))
pasta_templates = os.path.join(base_dir, 'template_admin')

admin_bp = Blueprint('admin', __name__, 
                     template_folder=pasta_templates, 
                     url_prefix='/admin')

# --- FUNÇÃO DE APOIO (BACKOFFICE SEGURANÇA) ---
def verificar_acesso_admin():
    """Retorna True se o usuário estiver autenticado e for administrador."""
    return current_user.is_authenticated and getattr(current_user, 'is_admin', False)

# --- ROTA 1: DASHBOARD ---
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
    return render_template('dashboard.html', status=status_sistema)

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
                engine_localidades = db.get_engine(bind='localidades')
                
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

                engine_loc = db.get_engine(bind='localidades')

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