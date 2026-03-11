import os
import json
import secrets
import threading
import time
import logging
import sys
from functools import wraps
from urllib.parse import urlencode
from datetime import datetime
from dotenv import load_dotenv
import requests

# Adiciona o diretório pai ao sys.path para resolver imports absolutos
diretorio_raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, diretorio_raiz)

# 1. Imports do Flask e Extensões Base
from flask import Flask, render_template, redirect, url_for, request, flash, abort, session
from flask_session import Session
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import login_user, login_required, logout_user, current_user
from app.extensions import db, login_manager
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from app.painel_admin.admin_routes import admin_bp
from sqlalchemy import text, func

# 2. Configuração de Caminho e Inicialização do App
# Carrega o ambiente baseado na variável de sistema APP_ENV (padrão: dev)
diretorio_atual = os.path.dirname(os.path.abspath(__file__)) #
# Adiciona o diretório pai ao sys.path para resolver imports absolutos
sys.path.insert(0, os.path.dirname(diretorio_atual))
env_name = os.getenv('APP_ENV', 'dev')
dotenv_path = os.path.join(diretorio_atual, f'.env.{env_name}')
load_dotenv(dotenv_path)


def resolve_indices_file_path():
    """Resolve caminho de índices com prioridade para storage persistente."""
    explicit_path = (os.getenv("INDICES_FILE_PATH") or "").strip()
    if explicit_path:
        return explicit_path
    render_disk = (os.getenv("RENDER_DISK_PATH") or "").strip()
    if render_disk:
        return os.path.join(render_disk, "indices.json")
    return os.path.join(diretorio_atual, 'indices.json')

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

# Configurações de Sessão (Filesystem é mais simples e funciona bem SEM reloader)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = 24 * 3600  # 24 horas
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP em dev
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Protege de XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Protege de CSRF

# Configuração para OAuth em HTTPS com auto-redirecionamento
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Permite HTTP em dev (NUNCA em PROD)

# Configurações de e-mail (para recuperação de senha)
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

mail = Mail(app)

def _get_serializer():
    secret_key = app.config['SECRET_KEY']
    return URLSafeTimedSerializer(secret_key, salt='password-reset-salt')

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

# Inicializar Flask-Session
session_mgr = Session(app)

# Configurações do Google OAuth (será usado manualmente)
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'
# URI de redirecionamento OAuth - deve ser EXATAMENTE a mesma no Google Cloud Console (Credenciais → URIs de redirecionamento)
REDIRECT_URI = os.getenv('GOOGLE_OAUTH_REDIRECT_URI', 'http://127.0.0.1:5000/login/google/callback')

app.register_blueprint(admin_bp) #

# 5. Imports que dependem do Contexto (Executados após a vinculação do DB)
with app.app_context(): #
    from app.models import User, DeParaLogistica, FreteReal, NoticiaPortal #
    from app.brain import processar_inteligencia_frete #
    from app.news_ai import registrar_lead_newsletter #

# Em produção com Gunicorn, o bloco __main__ não executa.
# Esta rotina garante criação do schema uma única vez por processo.
_schema_initialized = False
_schema_lock = threading.Lock()


def ensure_database_schema():
    global _schema_initialized
    if _schema_initialized:
        return

    with _schema_lock:
        if _schema_initialized:
            return
        try:
            # Cria primeiro o schema principal (bind padrão/auth), essencial para login.
            db.create_all(bind_key=[None])

            # Tenta criar schemas opcionais sem derrubar o app caso um caminho de bind esteja inválido.
            for optional_bind in ['localidades', 'historico', 'leads', 'noticias']:
                try:
                    db.create_all(bind_key=[optional_bind])
                except Exception as bind_error:
                    logging.warning(
                        f"Não foi possível inicializar bind '{optional_bind}': {bind_error}"
                    )

            _schema_initialized = True
            logging.info("Banco inicializado: tabelas verificadas/criadas com sucesso.")
        except Exception as e:
            logging.exception(f"Falha ao inicializar banco de dados: {e}")
            raise


def ensure_bootstrap_admin_user():
    """Promove um usuário existente a admin no startup, quando configurado."""
    admin_email = os.getenv('BOOTSTRAP_ADMIN_EMAIL') or os.getenv('MAIL_USERNAME')
    if not admin_email:
        return

    try:
        user = User.query.filter_by(email=admin_email).first()
        if not user:
            logging.info(f"Bootstrap admin: usuário '{admin_email}' ainda não existe.")
            return

        if user.is_admin:
            return

        user.is_admin = True
        db.session.commit()
        logging.info(f"Bootstrap admin: usuário '{admin_email}' promovido para admin.")
    except Exception as e:
        logging.exception(f"Falha ao promover usuário admin no bootstrap: {e}")


def get_admin_emails():
    """Retorna lista normalizada de e-mails com privilégio admin."""
    raw_admins = os.getenv('ADMIN_EMAILS', '')
    candidates = [e.strip().lower() for e in raw_admins.split(',') if e.strip()]

    bootstrap_admin = (os.getenv('BOOTSTRAP_ADMIN_EMAIL') or '').strip().lower()
    mail_username = (os.getenv('MAIL_USERNAME') or '').strip().lower()

    if bootstrap_admin:
        candidates.append(bootstrap_admin)
    if mail_username:
        candidates.append(mail_username)

    return set(candidates)


def normalize_email(email):
    """Normaliza e-mail para comparações estáveis no banco."""
    return (email or '').strip().lower()


def is_profile_complete(user):
    """Define perfil completo somente quando os dois campos obrigatórios têm conteúdo."""
    return bool((user.job_role or '').strip()) and bool((user.usage_purpose or '').strip())


with app.app_context():
    ensure_database_schema()
    ensure_bootstrap_admin_user()
      
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


def ops_token_required():
    """Protege endpoints operacionais com token compartilhado via ambiente."""
    expected = os.getenv('OPS_TOKEN', '').strip()
    provided = request.headers.get('X-Ops-Token', '').strip()
    if not expected or provided != expected:
        abort(403)

# --- ROTA DE DIAGNÓSTICO (TEMPORÁRIA) ---
@app.route('/oauth-diagnostics')
def oauth_diagnostics():
    """Rota de diagnóstico para verificar o estado do OAuth."""
    try:
        logging.info("=== DIAGNÓSTICO OAUTH ===")
        
        diagnostics = {
            "google_oauth_client_id": "✓ Configurado" if os.getenv('GOOGLE_OAUTH_CLIENT_ID') else "✗ NÃO CONFIGURADO",
            "google_oauth_client_secret": "✓ Configurado" if os.getenv('GOOGLE_OAUTH_CLIENT_SECRET') else "✗ NÃO CONFIGURADO",
            "flask_app_debug": app.debug,
            "oauth_fluxo": "manual (callback /login/google/callback)",
            "redirect_uri_esperado": REDIRECT_URI,
            "request_host": request.host,
            "request_url_root": request.url_root,
        }
        
        for key, value in diagnostics.items():
            logging.info(f"{key}: {value}")
        
        return {
            "status": "ok",
            "diagnostics": diagnostics,
            "message": "Verifique o terminal/logs para detalhes completos"
        }, 200
        
    except Exception as e:
        logging.error(f"Erro no diagnóstico: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}, 500


@app.route('/ops/user-audit', methods=['POST'])
def ops_user_audit():
    """Audita usuários para identificar possíveis duplicidades e status admin."""
    ops_token_required()
    ensure_database_schema()

    try:
        users = User.query.order_by(User.id.asc()).all()

        # Duplicidade por e-mail normalizado (case-insensitive)
        duplicate_groups = (
            db.session.query(func.lower(User.email).label('email_norm'), func.count(User.id).label('cnt'))
            .group_by(func.lower(User.email))
            .having(func.count(User.id) > 1)
            .all()
        )

        duplicates = []
        for group in duplicate_groups:
            members = (
                User.query.filter(func.lower(User.email) == group.email_norm)
                .order_by(User.id.asc())
                .all()
            )
            duplicates.append({
                'email_norm': group.email_norm,
                'count': int(group.cnt),
                'members': [
                    {
                        'id': u.id,
                        'email': u.email,
                        'is_admin': bool(u.is_admin),
                        'oauth_provider': u.oauth_provider,
                    }
                    for u in members
                ],
            })

        return {
            'status': 'ok',
            'total_users': len(users),
            'users': [
                {
                    'id': u.id,
                    'email': u.email,
                    'is_admin': bool(u.is_admin),
                    'oauth_provider': u.oauth_provider,
                }
                for u in users
            ],
            'duplicates': duplicates,
        }, 200
    except Exception as e:
        logging.exception(f'Erro em /ops/user-audit: {e}')
        return {'status': 'error', 'message': str(e)}, 500


@app.route('/ops/promote-admin', methods=['POST'])
def ops_promote_admin():
    """Promove usuário para admin a partir do e-mail informado."""
    ops_token_required()
    ensure_database_schema()

    payload = request.get_json(silent=True) or {}
    email = (payload.get('email') or '').strip().lower()
    if not email:
        return {'status': 'error', 'message': 'Campo email é obrigatório.'}, 400

    try:
        users = (
            User.query.filter(func.lower(User.email) == email)
            .order_by(User.id.asc())
            .all()
        )

        if not users:
            return {'status': 'error', 'message': 'Usuário não encontrado.'}, 404

        for user in users:
            user.is_admin = True

        db.session.commit()

        return {
            'status': 'ok',
            'promoted_count': len(users),
            'users': [
                {
                    'id': u.id,
                    'email': u.email,
                    'is_admin': bool(u.is_admin),
                }
                for u in users
            ],
        }, 200
    except Exception as e:
        db.session.rollback()
        logging.exception(f'Erro em /ops/promote-admin: {e}')
        return {'status': 'error', 'message': str(e)}, 500

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
    path_indices = resolve_indices_file_path()
    fallback_indicadores = {"dolar": "0.00", "petroleo": "0.00", "bdi": "-", "fbx": "-"}
    try:
        with open(path_indices, 'r', encoding='utf-8') as f:
            conteudo_indices = json.load(f)

        # Compatibilidade com os dois formatos:
        # 1) formato antigo: {"dolar", "petroleo", "bdi", "fbx"}
        # 2) formato historico: {"ultima_atualizacao", "historico": [...]} 
        if isinstance(conteudo_indices, dict) and isinstance(conteudo_indices.get('historico'), list):
            historico = conteudo_indices.get('historico') or []
            ultimo_registro = historico[-1] if historico else {}
            indicadores = {
                "dolar": ultimo_registro.get("dolar", fallback_indicadores["dolar"]),
                "petroleo": ultimo_registro.get("petroleo", fallback_indicadores["petroleo"]),
                "bdi": ultimo_registro.get("bdi", fallback_indicadores["bdi"]),
                "fbx": ultimo_registro.get("fbx", fallback_indicadores["fbx"]),
            }
        elif isinstance(conteudo_indices, dict):
            indicadores = {
                "dolar": conteudo_indices.get("dolar", fallback_indicadores["dolar"]),
                "petroleo": conteudo_indices.get("petroleo", fallback_indicadores["petroleo"]),
                "bdi": conteudo_indices.get("bdi", fallback_indicadores["bdi"]),
                "fbx": conteudo_indices.get("fbx", fallback_indicadores["fbx"]),
            }
        else:
            indicadores = fallback_indicadores
    except (FileNotFoundError, json.JSONDecodeError):
        indicadores = fallback_indicadores

    try:
        noticias_reais = NoticiaPortal.query.order_by(NoticiaPortal.data_publicacao.desc()).limit(10).all()
    except Exception as e:
        logging.error(f"Erro ao buscar notícias para a página inicial: {e}")
        noticias_reais = []

    return render_template('index.html', noticias=noticias_reais, indicadores=indicadores)

# --- Rota de Login Corrigida ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    logging.info(f"=== Acessando /login (método: {request.method}) ===")
    ensure_database_schema()
    if request.method == 'POST':
        email_input = request.form.get('email')
        password = request.form.get('password')
        logging.info(f"Tentativa de login com email: {email_input}")

        email_norm = normalize_email(email_input)
        user = User.query.filter(func.lower(User.email) == email_norm).first()

        if user and user.verify_password(password):
            user.last_login_at = datetime.utcnow()
            db.session.commit()

            login_user(user)
            if user.is_admin:
                return redirect(url_for('admin.admin_dashboard'))
            return redirect(url_for('index'))

        flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')


@app.route('/request-password-reset', methods=['GET', 'POST'])
def request_password_reset():
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        if not email:
            flash('Informe o e-mail cadastrado.', 'warning')
            return redirect(url_for('request_password_reset'))

        user = User.query.filter(func.lower(User.email) == email).first()
        if not user:
            # Não revelamos se o e-mail existe ou não, por segurança
            flash('Se o e-mail estiver cadastrado, enviaremos um link de recuperação.', 'info')
            return redirect(url_for('request_password_reset'))

        s = _get_serializer()
        token = s.dumps({'user_id': user.id})

        reset_url = url_for('reset_password', token=token, _external=True)
        subject = 'Redefinição de senha - Agentefrete'
        body = f'''Olá {user.full_name},

Recebemos uma solicitação para redefinir sua senha no Agentefrete.

Para criar uma nova senha, acesse o link abaixo (válido por 1 hora):
{reset_url}

Se você não solicitou esta redefinição, ignore este e-mail.

Atenciosamente,
Equipe Agentefrete
'''
        try:
            msg = Message(subject=subject, recipients=[user.email], body=body)
            mail.send(msg)
            flash('Se o e-mail estiver cadastrado, enviaremos um link de recuperação. Confira também a pasta de spam ou lixo eletrônico.', 'info')
            if app.debug:
                session['dev_reset_link'] = reset_url
                logging.info(f'[DEV] Link de redefinição (e-mail pode não chegar): {reset_url}')
        except Exception as e:
            logging.error(f'Erro ao enviar e-mail de recuperação de senha: {e}')
            flash('Não foi possível enviar o e-mail de recuperação. Tente novamente mais tarde.', 'danger')

        return redirect(url_for('request_password_reset'))

    dev_reset_link = session.pop('dev_reset_link', None)
    return render_template('request_reset.html', dev_reset_link=dev_reset_link)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    s = _get_serializer()
    try:
        data = s.loads(token, max_age=3600)  # 1 hora
        user_id = data.get('user_id')
    except SignatureExpired:
        flash('O link de redefinição expirou. Solicite novamente.', 'warning')
        return redirect(url_for('request_password_reset'))
    except BadSignature:
        flash('Link de redefinição inválido.', 'danger')
        return redirect(url_for('login'))

    user = db.session.get(User, user_id)
    if not user:
        flash('Usuário não encontrado.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not password or not confirm_password:
            flash('Preencha todos os campos.', 'warning')
            return redirect(url_for('reset_password', token=token))

        if password != confirm_password:
            flash('As senhas não conferem.', 'warning')
            return redirect(url_for('reset_password', token=token))

        user.set_password(password)
        db.session.commit()
        flash('Senha redefinida com sucesso. Faça login com a nova senha.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)

# --- ROTAS DE LOGIN OAUTH MANUAL (SEM FLASK-DANCE) ---

@app.route('/login/google')
def login_google():
    """Inicia o fluxo OAuth com o Google"""
    logging.info("=== Iniciando fluxo OAuth com Google ===")
    
    # Gerar state para proteção CSRF
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session.permanent = True
    
    params = {
        'client_id': GOOGLE_CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
    }
    # URL encode para que redirect_uri e demais valores sejam aceitos corretamente pelo Google
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    logging.info(f"Redirect URI enviado: {REDIRECT_URI}")
    return redirect(auth_url)


@app.route('/login/google/callback')
def google_callback():
    """Callback do Google OAuth"""
    logging.info("=== INICIANDO google_callback ===")
    ensure_database_schema()
    
    # Validar state (proteção CSRF)
    state = request.args.get('state')
    session_state = session.get('oauth_state')
    
    logging.info(f"State recebido: {state}")
    logging.info(f"State armazenado: {session_state}")
    
    if not state or state != session_state:
        logging.error("State inválido ou não encontrado - proteção CSRF falhada")
        flash('Falha na validação de segurança. Tente novamente.', 'danger')
        return redirect(url_for('login'))
    
    # Verificar se houve erro do Google
    error = request.args.get('error')
    if error:
        logging.error(f"Erro do Google: {error}")
        flash(f'Erro na autenticação: {error}', 'danger')
        return redirect(url_for('login'))
    
    # Obter authorization code
    code = request.args.get('code')
    if not code:
        logging.error("Authorization code não fornecido")
        flash('Authorization code não fornecido.', 'danger')
        return redirect(url_for('login'))
    
    logging.info(f"Authorization code recebido: {code[:20]}...")
    
    try:
        # Trocar code por token
        logging.info("Trocando code por token...")
        token_data = {
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': REDIRECT_URI,
        }
        
        token_response = requests.post(GOOGLE_TOKEN_URL, data=token_data)
        logging.info(f"Status da resposta de token: {token_response.status_code}")
        
        if token_response.status_code != 200:
            logging.error(f"Erro ao obter token: {token_response.text}")
            flash('Não foi possível obter o token de acesso.', 'danger')
            return redirect(url_for('login'))
        
        tokens = token_response.json()
        access_token = tokens.get('access_token')
        logging.info(f"Token de acesso obtido: {access_token[:20]}...")
        
        # Obter dados do usuário
        logging.info("Obtendo dados do usuário...")
        headers = {'Authorization': f'Bearer {access_token}'}
        userinfo_response = requests.get(GOOGLE_USERINFO_URL, headers=headers)
        logging.info(f"Status da resposta userinfo: {userinfo_response.status_code}")
        
        if userinfo_response.status_code != 200:
            logging.error(f"Erro ao obter userinfo: {userinfo_response.text}")
            flash('Não foi possível obter os dados do Google.', 'danger')
            return redirect(url_for('login'))
        
        user_data = userinfo_response.json()
        logging.info(f"Dados do usuário: email={user_data.get('email')}, name={user_data.get('name')}")
        
        email = normalize_email(user_data.get('email'))
        name = user_data.get('name') or user_data.get('given_name') or 'Usuário Google'
        google_id = user_data.get('id')
        admin_emails = get_admin_emails()
        
        if not email:
            logging.error("E-mail não fornecido pelo Google")
            flash('Sua conta Google não retornou um e-mail válido.', 'danger')
            return redirect(url_for('login'))
        
        # Buscar ou criar usuário (prioriza sub do Google, fallback por e-mail normalizado)
        user = None
        if google_id:
            user = User.query.filter_by(oauth_provider='google', oauth_sub=google_id).first()

        if not user:
            user = User.query.filter(func.lower(User.email) == email).order_by(User.id.asc()).first()
        
        if not user:
            logging.info(f"Criando novo usuário: {email}")
            user = User(
                email=email,
                full_name=name,
                is_admin=(email or '').strip().lower() in admin_emails,
                categoria='free',
                creditos=10,
                subscribes_to_newsletter=False,
                usage_purpose=None,
                job_role=None,
                oauth_provider='google',
                oauth_sub=google_id,
            )
            db.session.add(user)
        else:
            logging.info(f"Usuário existente encontrado: {email}")
            if not user.oauth_provider:
                user.oauth_provider = 'google'
                user.oauth_sub = google_id
            elif user.oauth_provider == 'google' and not user.oauth_sub and google_id:
                user.oauth_sub = google_id
            if (email or '').strip().lower() in admin_emails and not user.is_admin:
                user.is_admin = True
                logging.info(f"Usuário {email} promovido para admin via ADMIN_EMAILS.")
        
        user.last_login_at = datetime.utcnow()
        db.session.commit()
        logging.info("Usuário salvo no banco")
        
        # Fazer login
        login_user(user)
        logging.info(f"Usuário {email} autenticado com sucesso")
        flash('Login com Google realizado com sucesso.', 'success')
        
        # Limpar session state
        session.pop('oauth_state', None)
        
        # Verificar se o usuário precisa completar o perfil
        if not is_profile_complete(user):
            logging.info(f"Usuário {email} precisa completar perfil")
            session['pending_profile_completion'] = True
            return redirect(url_for('complete_profile'))
        
        if user.is_admin:
            logging.info("Redirecionando para dashboard admin")
            return redirect(url_for('admin.admin_dashboard'))
        
        logging.info("Redirecionando para index")
        return redirect(url_for('index'))
        
    except Exception as e:
        logging.error(f"ERRO no google_callback: {str(e)}", exc_info=True)
        flash(f'Erro no login com Google: {str(e)}', 'danger')
        return redirect(url_for('login'))

# --- Rota de Registro Corrigida ---
@app.route('/complete-profile', methods=['GET', 'POST'])
@login_required
def complete_profile():
    """Rota para completar o perfil do usuário após login via OAuth"""
    logging.info(f"=== Acessando /complete-profile (método: {request.method}) ===")
    
    user = current_user
    
    # Verificar se o usuário já tem esses dados preenchidos
    if request.method == 'GET' and is_profile_complete(user):
        logging.info(f"Usuário {user.email} já tem perfil completo. Redirecionando para index.")
        flash('Seu perfil já está completo.', 'info')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        job_role = (request.form.get('job_role') or '').strip()
        usage_purpose = (request.form.get('usage_purpose') or '').strip()
        subscribes_to_newsletter = bool(request.form.get('subscribes_to_newsletter'))
        
        if not job_role or not usage_purpose:
            flash('Por favor, preencha todos os campos obrigatórios.', 'danger')
            return redirect(url_for('complete_profile'))
        
        # Atualizar dados do usuário
        user.job_role = job_role
        user.usage_purpose = usage_purpose
        user.subscribes_to_newsletter = subscribes_to_newsletter
        
        db.session.commit()
        logging.info(f"Perfil do usuário {user.email} atualizado com sucesso")
        
        # Limpar flag de sessão
        session.pop('pending_profile_completion', None)
        
        flash('Perfil completado com sucesso! Bem-vindo!', 'success')
        
        if user.is_admin:
            return redirect(url_for('admin.admin_dashboard'))
        return redirect(url_for('index'))
    
    # GET: Exibir formulário
    return render_template('complete_profile.html')

@app.route('/register', methods=['POST'])
def register():
    full_name = (request.form.get('nome') or '').strip()
    email = normalize_email(request.form.get('email'))
    password = request.form.get('password')
    job_role = (request.form.get('job_role') or '').strip()
    usage_purpose = (request.form.get('usage_purpose') or '').strip()
    subscribes_to_newsletter = bool(request.form.get('subscribes_to_newsletter'))

    if not full_name or not email or not password:
        flash('Por favor, preencha nome, e-mail e senha.', 'danger')
        return redirect(url_for('login'))

    user_exists = User.query.filter(func.lower(User.email) == email).first()
    if user_exists:
        flash('Este e-mail já está cadastrado.', 'danger')
        return redirect(url_for('login'))

    new_user = User(
        email=email,
        full_name=full_name,
        is_admin=False,
        categoria='free',
        creditos=10,
        subscribes_to_newsletter=subscribes_to_newsletter,
        usage_purpose=usage_purpose,
        job_role=job_role,
    )
    new_user.set_password(password)

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
    # use_reloader=False é CRÍTICO para OAuth2 em desenvolvimento
    # (o reloader reinicia o app e invalida a sessão durante o callback do Google)
    app.run(debug=True, use_reloader=False, threaded=True)
