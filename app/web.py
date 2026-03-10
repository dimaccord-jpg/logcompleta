import os
import sys

# Garante que o pacote 'app' seja encontrado ao rodar este arquivo como script (ex.: python app/web.py)
_diretorio_app = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(_diretorio_app) not in sys.path:
    sys.path.insert(0, os.path.dirname(_diretorio_app))

import json
import logging
from urllib.parse import urlparse

# 1. Imports do Flask e Extensões Base
from flask import Flask, render_template, redirect, url_for, request, flash, abort, session
from flask_session import Session
from flask_login import login_user, login_required, logout_user, current_user
from app.extensions import db, login_manager
from flask_mail import Mail
from app.painel_admin.admin_routes import admin_bp
from app.ops_routes import ops_bp
from app.infra import (
    resolve_sqlite_path,
    ensure_database_schema,
    ensure_bootstrap_admin_user,
    get_user_by_id,
    admin_required,
)
from app.auth_services import (
    authenticate_user,
    request_password_reset as auth_request_password_reset,
    get_user_for_reset_token,
    reset_password_with_token as auth_reset_password_with_token,
    get_google_oauth_login_url,
    handle_google_oauth_callback,
    complete_user_profile as auth_complete_user_profile,
    register_user,
)

# 2. Configuração de ambiente e dotenv (via loader centralizado)
from app import env_loader

_env_loaded = env_loader.load_app_env()

# Configuração de Logging (Global para o Flask e Gunicorn)
log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s | %(levelname)s | FLASK_APP | %(message)s',
    stream=sys.stdout
)

if not _env_loaded:
    _env_name_for_log = (os.getenv('APP_ENV', 'dev') or 'dev').strip() or 'dev'
    _dotenv_path_for_log = os.path.join(env_loader.get_app_dir(), f'.env.{_env_name_for_log}')
    logging.warning(
        "Arquivo de ambiente .env.%s não foi encontrado/carregado em %s. "
        "Seguindo apenas com variáveis já definidas no ambiente.",
        _env_name_for_log,
        _dotenv_path_for_log,
    )

app = Flask(__name__)

# 3. Configurações de Segurança e Banco de Dados (OBRIGATÓRIO ANTES DO INIT_APP)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'chave_insegura_padrao_dev')
db_uri_auth = os.getenv('DB_URI_AUTH', 'sqlite:///' + os.path.join(_diretorio_app, 'painel_admin', 'auth.db'))
app.config['SQLALCHEMY_DATABASE_URI'] = resolve_sqlite_path(db_uri_auth, _diretorio_app)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() in ('true', '1', 't')
app.config['JULIA_AVATAR_URL'] = (os.getenv('JULIA_AVATAR_URL', '') or '').strip()

# Configurações de Sessão (Filesystem é mais simples e funciona bem SEM reloader)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = 24 * 3600  # 24 horas
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP em dev
app.config['SESSION_COOKIE_HTTPONLY'] = True  # Protege de XSS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # Protege de CSRF

# Configuração para OAuth em HTTPS com auto-redirecionamento
# Só permite OAuth em HTTP quando explicitado no .env (ex.: .env.dev). Em prod/homolog não definir ou usar 0.
if os.getenv('OAUTHLIB_INSECURE_TRANSPORT', '').strip().lower() in ('1', 'true', 't'):
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
else:
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '0'

# Configurações de e-mail (para recuperação de senha)
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() in ('true', '1', 't')
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

mail = Mail(app)

# Configuração dos Binds (Bancos adicionais)
# Tenta pegar do .env, se não existir, usa o caminho padrão relativo (fallback)
db_binds = {
    'localidades': os.getenv('DB_URI_LOCALIDADES', 'sqlite:///' + os.path.join(_diretorio_app, 'base_localidades.db')),
    'historico':   os.getenv('DB_URI_HISTORICO', 'sqlite:///' + os.path.join(_diretorio_app, 'historico_frete.db')),
    'leads':       os.getenv('DB_URI_LEADS', 'sqlite:///' + os.path.join(_diretorio_app, 'leads.db')),
    'noticias':    os.getenv('DB_URI_NOTICIAS', 'sqlite:///' + os.path.join(_diretorio_app, 'noticias.db')),
    'gerencial':   os.getenv('DB_URI_GERENCIAL', 'sqlite:///' + os.path.join(_diretorio_app, 'gerencial.db'))
}
app.config['SQLALCHEMY_BINDS'] = {k: resolve_sqlite_path(v, _diretorio_app) for k, v in db_binds.items()}

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

app.register_blueprint(admin_bp)
app.register_blueprint(ops_bp)

# 5. Imports que dependem do Contexto (Executados após a vinculação do DB)
with app.app_context():
    from app.models import User, DeParaLogistica, FreteReal, NoticiaPortal
    from app.brain import processar_inteligencia_frete
    from app.news_ai import registrar_lead_newsletter

with app.app_context():
    ensure_database_schema(db)
    ensure_bootstrap_admin_user(db)


@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(user_id)

# --- ROTAS PÚBLICAS E ACESSO ---
@app.route('/')
def index():
    # Localiza o arquivo de índices dinâmicos
    path_indices = os.path.join(_diretorio_app, 'indices.json')
    try:
        with open(path_indices, 'r', encoding='utf-8') as f:
            indicadores = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        indicadores = {"dolar": "0.00", "petroleo": "0.00", "bdi": "-", "fbx": "-"}

    try:
        noticias_reais = NoticiaPortal.query.order_by(NoticiaPortal.data_publicacao.desc()).limit(10).all()
    except Exception as e:
        logging.error(f"Erro ao buscar notícias para a página inicial: {e}")
        noticias_reais = []

    return render_template('index.html', noticias=noticias_reais, indicadores=indicadores)

# --- Login (delegação para auth_services) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    logging.info("=== Acessando /login (método: %s) ===", request.method)
    ensure_database_schema(db)
    if request.method == 'POST':
        email_input = request.form.get('email')
        password = request.form.get('password')
        logging.info("Tentativa de login com email: %s", email_input)
        user, error = authenticate_user(email_input or "", password or "")
        if user:
            login_user(user)
            if user.is_admin:
                return redirect(url_for('admin.admin_dashboard'))
            return redirect(url_for('index'))
        flash(error or 'Email ou senha incorretos.', 'danger')
    return render_template('login.html')


@app.route('/request-password-reset', methods=['GET', 'POST'])
def request_password_reset():
    if request.method == 'POST':
        email = request.form.get('email') or ""
        success, message, dev_reset_url = auth_request_password_reset(
            email,
            secret_key=app.config['SECRET_KEY'],
            mail=mail,
            build_reset_url=lambda token: url_for('reset_password', token=token, _external=True),
        )
        flash(message, 'info' if success else 'danger')
        if success and dev_reset_url and app.debug:
            session['dev_reset_link'] = dev_reset_url
            logging.info('[DEV] Link de redefinição (e-mail pode não chegar): %s', dev_reset_url)
        return redirect(url_for('request_password_reset'))
    dev_reset_link = session.pop('dev_reset_link', None)
    return render_template('request_reset.html', dev_reset_link=dev_reset_link)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if request.method == 'POST':
        password = request.form.get('password') or ""
        confirm_password = request.form.get('confirm_password') or ""
        success, message, redirect_view = auth_reset_password_with_token(
            token, password, confirm_password, secret_key=app.config['SECRET_KEY']
        )
        flash(message, 'success' if success else 'warning')
        if redirect_view:
            return redirect(url_for(redirect_view))
        return redirect(url_for('reset_password', token=token))
    user, err_msg, redirect_view = get_user_for_reset_token(token, secret_key=app.config['SECRET_KEY'])
    if user is None:
        flash(err_msg, 'danger' if redirect_view == 'login' else 'warning')
        return redirect(url_for(redirect_view))
    return render_template('reset_password.html', token=token)

# --- OAuth Google (delegação para auth_services) ---
@app.route('/login/google')
def login_google():
    """Inicia o fluxo OAuth com o Google"""
    logging.info("=== Iniciando fluxo OAuth com Google ===")
    auth_url, state = get_google_oauth_login_url(
        client_id=GOOGLE_CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        auth_url=GOOGLE_AUTH_URL,
    )
    session['oauth_state'] = state
    session.permanent = True
    logging.info("Redirect URI enviado: %s", REDIRECT_URI)
    return redirect(auth_url)


@app.route('/login/google/callback')
def google_callback():
    """Callback do Google OAuth"""
    logging.info("=== INICIANDO google_callback ===")
    ensure_database_schema(db)
    state = request.args.get('state')
    session_state = session.get('oauth_state')
    error = request.args.get('error')
    if error:
        logging.error("Erro do Google: %s", error)
        flash('Erro na autenticação: %s' % error, 'danger')
        return redirect(url_for('login'))
    code = request.args.get('code')
    user, err_msg, needs_profile = handle_google_oauth_callback(
        code or "",
        state or "",
        session_state or "",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        token_url=GOOGLE_TOKEN_URL,
        userinfo_url=GOOGLE_USERINFO_URL,
    )
    if user is None:
        flash(err_msg or 'Erro no login com Google.', 'danger')
        return redirect(url_for('login'))
    login_user(user)
    flash('Login com Google realizado com sucesso.', 'success')
    session.pop('oauth_state', None)
    if needs_profile:
        session['pending_profile_completion'] = True
        return redirect(url_for('complete_profile'))
    if user.is_admin:
        return redirect(url_for('admin.admin_dashboard'))
    return redirect(url_for('index'))

@app.route('/complete-profile', methods=['GET', 'POST'])
@login_required
def complete_profile():
    """Rota para completar o perfil do usuário após login via OAuth"""
    logging.info("=== Acessando /complete-profile (método: %s) ===", request.method)
    user = current_user
    if request.method == 'GET' and (user.job_role or '').strip() and (user.usage_purpose or '').strip():
        flash('Seu perfil já está completo.', 'info')
        return redirect(url_for('index'))
    if request.method == 'POST':
        job_role = (request.form.get('job_role') or '').strip()
        usage_purpose = (request.form.get('usage_purpose') or '').strip()
        subscribes_to_newsletter = bool(request.form.get('subscribes_to_newsletter'))
        success, message = auth_complete_user_profile(user, job_role, usage_purpose, subscribes_to_newsletter)
        flash(message, 'success' if success else 'danger')
        if not success:
            return redirect(url_for('complete_profile'))
        session.pop('pending_profile_completion', None)
        if user.is_admin:
            return redirect(url_for('admin.admin_dashboard'))
        return redirect(url_for('index'))
    return render_template('complete_profile.html')


@app.route('/register', methods=['POST'])
def register():
    full_name = request.form.get('nome') or ""
    email = request.form.get('email') or ""
    password = request.form.get('password') or ""
    job_role = request.form.get('job_role') or ""
    usage_purpose = request.form.get('usage_purpose') or ""
    subscribes_to_newsletter = bool(request.form.get('subscribes_to_newsletter'))
    new_user, error = register_user(
        full_name, email, password,
        job_role=job_role,
        usage_purpose=usage_purpose,
        subscribes_to_newsletter=subscribes_to_newsletter,
    )
    if new_user is None:
        flash(error or 'Erro ao cadastrar.', 'danger')
        return redirect(url_for('login'))
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
        with open(os.path.join(_diretorio_app, 'indices.json'), 'r', encoding='utf-8') as f:
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

    def _resolver_url_imagem(raw_url: str | None) -> str | None:
        """Converte caminhos locais de imagem para URL pública de static com url_for."""
        val = (raw_url or "").strip()
        if not val:
            return None
        # URLs remotas ou data-uri seguem como vieram.
        parsed = urlparse(val)
        if parsed.scheme in ("http", "https", "data"):
            return val
        local = val.replace("\\", "/")
        if local.startswith("/static/"):
            return url_for("static", filename=local[len("/static/"):])
        if local.startswith("static/"):
            return url_for("static", filename=local[len("static/"):])
        if local.startswith("generated/"):
            return url_for("static", filename=local)
        return val

    url_imagem_resolvida = _resolver_url_imagem(noticia.url_imagem)
    
    # Redirecionamos ambos para o mesmo template, 
    # pois ele já gerencia a lógica de exibição interna.
    return render_template('noticia_interna.html', noticia=noticia, url_imagem_resolvida=url_imagem_resolvida)

# Criando lazy: importar dentro da função, na hora que você realmente vai usar.
@app.route("/executar-cleiton", methods=["POST"])
@login_required
def executar_cleiton():
    # Import LAZY: só acontece quando você chama esse endpoint
    from app.run_cleiton import executar_orquestracao

    executar_orquestracao(app)
    flash("Cleiton executado com sucesso.", "success")
    return redirect(url_for("index"))


# Fase 5: Customer Insight (delegação; lógica em run_cleiton_agente_customer_insight)
@app.route("/executar-insight", methods=["POST"])
@login_required
def executar_insight():
    # Mantemos esta rota por compatibilidade, mas preservamos o objetivo principal:
    # insight roda ao final do ciclo gerencial completo do Cleiton.
    from app.run_cleiton import executar_orquestracao
    executar_orquestracao(app)
    flash("Ciclo do Cleiton executado (inclui Customer Insight no final).", "success")
    return redirect(url_for("index"))


# --- CRON: execução automática do ciclo Cleiton (Render Cron Job / agendador externo) ---
@app.route("/cron/executar-cleiton", methods=["GET", "POST"])
def cron_executar_cleiton():
    """
    Rota para agendador (ex.: Render Cron Job). Respeita frequência e janela; não usa bypass.
    Protegida por CRON_SECRET: header X-Cron-Secret ou query ?secret=<CRON_SECRET>.
    """
    secret = request.headers.get("X-Cron-Secret") or request.args.get("secret")
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if not expected or secret != expected:
        return {"ok": False, "error": "unauthorized"}, 403
    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(app, bypass_frequencia=False) or {}
        status = resultado.get("status", "falha")
        return {
            "ok": status == "sucesso",
            "status": status,
            "motivo": resultado.get("motivo", ""),
            "mission_id": resultado.get("mission_id"),
        }, 200
    except Exception as e:
        logging.exception("Cron executar-cleiton: %s", e)
        return {"ok": False, "error": str(e)}, 500


# --- EXECUÇÃO E MANUTENÇÃO ---
if __name__ == '__main__':
    # use_reloader=False é CRÍTICO para OAuth2 em desenvolvimento
    # (o reloader reinicia o app e invalida a sessão durante o callback do Google)
    app.run(debug=True, use_reloader=False, threaded=True)
