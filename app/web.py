import os
import json
import threading 
import time
import logging
import sys
from functools import wraps
from dotenv import load_dotenv

# 1. Imports do Flask e Extensões Base
from flask import Flask, render_template, redirect, url_for, request, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import login_user, login_required, logout_user, current_user
from extensions import db, login_manager
from painel_admin.admin_routes import admin_bp
from sqlalchemy import text

# 2. Configuração de Caminho e Inicialização do App
# Carrega o ambiente baseado na variável de sistema APP_ENV (padrão: dev)
diretorio_atual = os.path.dirname(os.path.abspath(__file__)) #
env_name = os.getenv('APP_ENV', 'dev')
dotenv_path = os.path.join(diretorio_atual, f'.env.{env_name}')
load_dotenv(dotenv_path)

# Configuração de Logging (Global para o Flask e Gunicorn)
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s | %(levelname)s | FLASK_APP | %(message)s',
    stream=sys.stdout
)

app = Flask(__name__) #

# Helper para resolver caminhos de banco de dados SQLite
def resolve_sqlite_path(uri, base_dir):
    """Converte URIs relativas de SQLite em absolutas e garante que o diretório exista."""
    if uri and uri.startswith('sqlite:///'):
        path_part = uri[len('sqlite:///'):]
        # Garante que caminhos relativos (sem / ou C:) sejam absolutizados
        if not os.path.isabs(path_part):
            absolute_path = os.path.join(base_dir, path_part)
            os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
            # Normaliza para forward slashes para compatibilidade Windows/SQLAlchemy
            return 'sqlite:///' + absolute_path.replace('\\', '/')
    return uri

# 3. Configurações de Segurança e Banco de Dados (OBRIGATÓRIO ANTES DO INIT_APP)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'chave_insegura_padrao_dev')
db_uri_auth = os.getenv('DB_URI_AUTH', 'sqlite:///' + os.path.join(diretorio_atual, 'painel_admin', 'auth.db'))
app.config['SQLALCHEMY_DATABASE_URI'] = resolve_sqlite_path(db_uri_auth, diretorio_atual)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False #
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')

# Configuração dos Binds (Bancos adicionais)
# Tenta pegar do .env, se não existir, usa o caminho padrão relativo (fallback)
db_binds = {
    'localidades': os.getenv('DB_URI_LOCALIDADES', 'sqlite:///' + os.path.join(diretorio_atual, 'base_localidades.db')),
    'historico':   os.getenv('DB_URI_HISTORICO', 'sqlite:///' + os.path.join(diretorio_atual, 'historico_frete.db')),
    'leads':       os.getenv('DB_URI_LEADS', 'sqlite:///' + os.path.join(diretorio_atual, 'leads.db')),
    'noticias':    os.getenv('DB_URI_NOTICIAS', 'sqlite:///' + os.path.join(diretorio_atual, 'noticias.db'))
}
app.config['SQLALCHEMY_BINDS'] = {k: resolve_sqlite_path(v, diretorio_atual) for k, v in db_binds.items()}

# 4. Inicializar extensões e Blueprints
db.init_app(app) # Agora o db encontrará as configurações acima
login_manager.init_app(app) #
app.register_blueprint(admin_bp) #

# 5. Imports que dependem do Contexto (Executados após a vinculação do DB)
with app.app_context(): #
    from models import User, DeParaLogistica, FreteReal, NoticiaPortal #
    from brain import processar_inteligencia_frete #
    from news_ai import registrar_lead_newsletter #
      
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# --- DECORADOR DE SEGURANÇA ---

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Acesso restrito apenas para administradores.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROTA DE HEALTH CHECK (MONITORAMENTO) ---
@app.route('/health')
def health_check():
    """Endpoint para verificar se a aplicação está viva (usado em Prod/Homolog)."""
    db_status = "unknown"
    try:
        # Teste rápido de conexão com o banco (timeout curto idealmente)
        db.session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return {
        "status": "ok",
        "ambiente": env_name,
        "database": db_status,
        "timestamp": time.time()
    }, 200

# --- ROTAS PÚBLICAS E ACESSO ---
@app.route('/')
def index():
    # Localiza o arquivo de índices dinâmicos
    path_indices = os.path.join(diretorio_atual, 'indices.json')
    try:
        with open(path_indices, 'r') as f:indicadores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):indicadores = {"dolar": "0.00", "petroleo": "0.00", "bdi": "-", "fbx": "-"}
    noticias_reais = NoticiaPortal.query.order_by(NoticiaPortal.data_publicacao.desc()).limit(10).all()

    return render_template('index.html', noticias=noticias_reais, indicadores=indicadores)

# --- Rota de Login Corrigida ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Mantemos 'email' para ler o que vem do seu HTML atual
        email_input = request.form.get('email')
        password = request.form.get('password')
        
        # BUSCA: Filtramos a coluna 'username' usando o valor do input de email
        user = User.query.filter_by(username=email_input).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            if user.is_admin:
                return redirect(url_for('admin.admin_dashboard'))
            return redirect(url_for('index'))
        
        flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')

# --- Rota de Registro Corrigida ---
@app.route('/register', methods=['POST'])
def register():
    email = request.form.get('email')
    password = request.form.get('password')
    
    # BUSCA: Verifica na coluna 'username'
    user_exists = User.query.filter_by(username=email).first()
    if user_exists:
        flash('Este e-mail já está cadastrado.', 'danger')
        return redirect(url_for('login'))
    
    # CRIAÇÃO: Mapeia o email para o campo username do Models.py
    new_user = User(
        username=email, # <--- Aqui o 'email' entra na coluna 'username'
        password=generate_password_hash(password, method='pbkdf2:sha256'),
        is_admin=False,
        categoria='free',
        creditos=10
    )
    
    db.session.add(new_user)
    db.session.commit()
    flash('Conta criada com sucesso! Faça login.', 'success')
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

# --- ROTAS DE INTELIGÊNCIA (CONECTADAS AO BRAIN) ---

@app.route('/fretes', methods=['GET', 'POST'])
@login_required
def fretes():
    # ALTERAÇÃO 1: Carregar os dados reais do indices.json
    try:
        with open(os.path.join(diretorio_atual, 'indices.json'), 'r', encoding='utf-8') as f:
            indices = json.load(f)
    except Exception:
        # Fallback de segurança caso o arquivo não exista ainda
        indices = {"ultima_atualizacao": "N/A", "historico": []}

    resultado = None
    
    if request.method == 'POST':
        # CAPTURA DOS DADOS DO FORMULÁRIO
        origem = request.form.get('origem')
        destino = request.form.get('destino')
        uf_o = request.form.get('uf_origem')
        uf_d = request.form.get('uf_destino')
        
        models = {
            'DeParaLogistica': DeParaLogistica,
            'FreteReal': FreteReal
        }
        
        # CHAMA O BRAIN PASSANDO AS UFs E OS ÍNDICES REAIS
        # ALTERAÇÃO 2: Passamos 'indices' para o processamento (verifique se seu brain.py já aceita este argumento)
        resultado_calculo, erro = processar_inteligencia_frete(
            origem, destino, uf_o, uf_d, models
        )
        
        if erro:
            flash(erro, "warning")
        else:
            resultado = resultado_calculo

    # Mantemos o retorno original passando os índices reais para o template
    return render_template('fretes.html', indices=indices, resultado=resultado)

# Rota para newsletter

@app.route('/inscrever-newsletter', methods=['POST'])
def inscrever_newsletter():
    email = request.form.get('email')
    # O web.py apenas repassa o e-mail, o news_ai faz o trabalho pesado
    sucesso, mensagem = registrar_lead_newsletter(email)
    
    flash(mensagem, "success" if sucesso else "danger")
    return redirect(url_for('index'))

# --- OUTROS MÓDULOS (PLACEHOLDERS) ---

@app.route('/analise')
@login_required
def analise():
    return "Módulo de Análise (Em breve)"

# --- Rota para link dinâminco de notícias

@app.route('/noticia/<int:noticia_id>')
def detalhe_noticia(noticia_id):
    # Busca a notícia específica no banco pelo ID
    noticia = NoticiaPortal.query.get_or_404(noticia_id)
    
    # Redirecionamos ambos para o mesmo template, 
    # pois ele já gerencia a lógica de exibição interna.
    return render_template('noticia_interna.html', noticia=noticia)

# Criando lazy: importar dentro da função, na hora que você realmente vai usar.
@app.route("/executar-cleiton", methods=["POST"])
@login_required
def executar_cleiton():
    # Import LAZY: só acontece quando você chama esse endpoint
    from run_cleiton import executar_orquestracao

    executar_orquestracao(app)
    flash("Cleiton executado com sucesso.", "success")
    return redirect(url_for("index"))

# --- EXECUÇÃO E MANUTENÇÃO ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  
    app.run(debug=True)
