import os
import logging
logging.basicConfig(level=logging.INFO)
logging.info("[VALIDAÇÃO] APP_DATA_DIR: %s", os.environ.get("APP_DATA_DIR"))
import sys

# Garante que o pacote 'app' seja encontrado ao rodar este arquivo como script (ex.: python app/web.py)
_diretorio_app = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(_diretorio_app) not in sys.path:
    sys.path.insert(0, os.path.dirname(_diretorio_app))

import json
import logging
from urllib.parse import urlparse
from pathlib import Path

from sqlalchemy import text

# 1. Imports do Flask e Extensões Base
from flask import Flask, render_template, redirect, url_for, request, flash, abort, session
from flask_session import Session
from flask_login import login_user, login_required, logout_user, current_user
from app.extensions import db, login_manager
from app.painel_admin.admin_routes import admin_bp
from app.ops_routes import ops_bp
from app.user_area import user_bp
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

# 2. Configuração de ambiente centralizada
from app.settings import settings



# Define diretorio_atual para uso em resolve_indices_file_path
diretorio_atual = os.path.dirname(os.path.abspath(__file__))

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
log_level = settings.log_level
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s | %(levelname)s | FLASK_APP | %(message)s',
    stream=sys.stdout
)

app = Flask(__name__)

_diretorio_dados = settings.data_dir
app.config["DATA_DIR"] = settings.data_dir  # usado pelo Admin para persistir última execução manual

# 3. Configurações de Segurança e Banco de Dados (OBRIGATÓRIO ANTES DO INIT_APP)
app.config['SECRET_KEY'] = settings.secret_key
app.config['SQLALCHEMY_DATABASE_URI'] = settings.sqlalchemy_database_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DEBUG'] = settings.debug
app.config['JULIA_AVATAR_URL'] = (os.getenv('JULIA_AVATAR_URL', '') or '').strip()

# Configurações de Sessão (Filesystem é mais simples e funciona bem SEM reloader)
app.config['SESSION_TYPE'] = settings.session_type
app.config['PERMANENT_SESSION_LIFETIME'] = settings.session_lifetime_seconds
app.config['SESSION_COOKIE_SECURE'] = settings.session_cookie_secure
app.config['SESSION_COOKIE_HTTPONLY'] = settings.session_cookie_httponly
app.config['SESSION_COOKIE_SAMESITE'] = settings.session_cookie_samesite

# Configuração para OAuth em HTTPS com auto-redirecionamento
# Só permite OAuth em HTTP quando explicitado no .env (ex.: .env.dev). Em prod/homolog não definir ou usar 0.
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' if settings.oauth_insecure_transport else '0'

# Configuração dos Binds (Bancos adicionais)
app.config['SQLALCHEMY_BINDS'] = settings.sqlalchemy_binds

# 4. Inicializar extensões e Blueprints
db.init_app(app)
login_manager.init_app(app)

# Inicializar Flask-Session
session_mgr = Session(app)

# Configurações do Google OAuth (será usado manualmente)
GOOGLE_CLIENT_ID = settings.google_client_id
GOOGLE_CLIENT_SECRET = settings.google_client_secret
GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'
# URI de redirecionamento OAuth - deve ser EXATAMENTE a mesma no Google Cloud Console (Credenciais → URIs de redirecionamento)
REDIRECT_URI = settings.google_redirect_uri

app.register_blueprint(admin_bp)
app.register_blueprint(ops_bp)
app.register_blueprint(user_bp)

# 5. Imports que dependem do Contexto (Executados após a vinculação do DB)
with app.app_context():
    from app.models import User, DeParaLogistica, FreteReal, NoticiaPortal
    from app.brain import processar_inteligencia_frete
    from app.news_ai import registrar_lead_newsletter
    from app.terms_services import get_active_term

with app.app_context():
    ensure_database_schema(db)
    ensure_bootstrap_admin_user(db)


@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(user_id)

# --- ROTAS PÚBLICAS E ACESSO ---
@app.route('/')
def index():
    # Localiza o arquivo de índices dinâmicos com fallback de caminhos legados.
    path_indices = settings.indices_file_path
    candidate_paths = [
        path_indices,
        '/var/data/indices.json',
        os.path.join(_diretorio_app, 'indices.json'),
    ]
    ordered_paths = []
    for p in candidate_paths:
        if p and p not in ordered_paths:
            ordered_paths.append(p)

    fallback_indicadores = {"dolar": "0.00", "petroleo": "0.00", "bdi": "-", "fbx": "-"}
    indicadores = fallback_indicadores
    for p in ordered_paths:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                conteudo_indices = json.load(f)
            # Compatibilidade com múltiplos formatos:
            # 1) formato histórico: {"ultima_atualizacao", "historico": [...]}
            # 2) formato antigo:    {"dolar", "petroleo", "bdi", "fbx"}
            # 3) formato lista:     [{"data", "dolar", "petroleo", "bdi", "fbx"}, ...]
            ultimo_registro = None

            if isinstance(conteudo_indices, dict) and isinstance(conteudo_indices.get('historico'), list):
                historico = conteudo_indices.get('historico') or []
                if not historico:
                    continue
                ultimo_registro = historico[-1]
            elif isinstance(conteudo_indices, dict):
                ultimo_registro = conteudo_indices
            elif isinstance(conteudo_indices, list) and conteudo_indices:
                ultimo_registro = conteudo_indices[-1]

            if ultimo_registro:
                indicadores = {
                    "dolar": ultimo_registro.get("dolar", fallback_indicadores["dolar"]),
                    "petroleo": ultimo_registro.get("petroleo", fallback_indicadores["petroleo"]),
                    "bdi": ultimo_registro.get("bdi", fallback_indicadores["bdi"]),
                    "fbx": ultimo_registro.get("fbx", fallback_indicadores["fbx"]),
                }
                break
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            continue

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
    return render_template('login.html', active_term=get_active_term())


@app.route('/request-password-reset', methods=['GET', 'POST'])
def request_password_reset():
    if request.method == 'POST':
        email = request.form.get('email') or ""
        success, message, dev_reset_url = auth_request_password_reset(
            email,
            secret_key=app.config['SECRET_KEY'],
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
    # Mantem compatibilidade com a chave legada e suporta multiplos fluxos iniciados
    # na mesma sessao (ex.: duplo clique), evitando falso negativo de CSRF no callback.
    session['oauth_state'] = state
    pending_states = session.get('oauth_states') or []
    if not isinstance(pending_states, list):
        pending_states = []
    pending_states.append(state)
    # Limita historico para evitar crescimento indefinido na sessao.
    session['oauth_states'] = pending_states[-5:]
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
    session_states = session.get('oauth_states') or []
    if not isinstance(session_states, list):
        session_states = []
    # Se o state de callback existir na lista pendente, usa-o como state de sessao.
    # Isso evita quebra quando outro fluxo OAuth atualizou oauth_state antes do retorno.
    if state and state in session_states:
        session_state = state
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
    if state and state in session_states:
        session_states = [s for s in session_states if s != state]
        if session_states:
            session['oauth_states'] = session_states
        else:
            session.pop('oauth_states', None)
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
        accept_terms = bool(request.form.get('accept_terms'))
        if not accept_terms:
            flash('É obrigatório aceitar os Termos de Uso para continuar.', 'danger')
            return redirect(url_for('complete_profile'))
        job_role = (request.form.get('job_role') or '').strip()
        usage_purpose = (request.form.get('usage_purpose') or '').strip()
        subscribes_to_newsletter = bool(request.form.get('subscribes_to_newsletter'))
        success, message = auth_complete_user_profile(
            user, job_role, usage_purpose, subscribes_to_newsletter, accept_terms=True
        )
        flash(message, 'success' if success else 'danger')
        if not success:
            return redirect(url_for('complete_profile'))
        session.pop('pending_profile_completion', None)
        if user.is_admin:
            return redirect(url_for('admin.admin_dashboard'))
        return redirect(url_for('index'))
    return render_template('complete_profile.html', active_term=get_active_term())


@app.route('/register', methods=['POST'])
def register():
    accept_terms = bool(request.form.get('accept_terms'))
    if not accept_terms:
        flash('É obrigatório aceitar os Termos de Uso para criar sua conta.', 'danger')
        return redirect(url_for('login'))
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
        accept_terms=True,
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


@app.route("/health/liveness")
def health_liveness():
    """
    Indica apenas se o processo web está vivo.
    Não verifica dependências externas.
    """
    return {
        "status": "ok",
        "app_env": settings.app_env,
    }, 200


@app.route("/health/readiness")
def health_readiness():
    """
    Verifica dependências essenciais: banco default e acesso ao armazenamento de índices.
    Em caso de falha parcial, responde 503 mas não derruba o processo.
    """
    checks = {}
    ok = True

    # Banco de dados principal
    try:
        db.session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        logging.exception("Healthcheck readiness: falha ao acessar banco principal: %s", e)
        checks["database"] = f"error: {e}"
        ok = False

    # Armazenamento de índices (fase 1 ainda em arquivo)
    try:
        idx_path = Path(settings.indices_file_path)
        checks["indices_path"] = str(idx_path)
        checks["indices_exists"] = idx_path.exists()
    except Exception as e:
        logging.exception("Healthcheck readiness: falha ao inspecionar indices: %s", e)
        checks["indices_error"] = str(e)
        ok = False

    status = "ok" if ok else "degraded"
    return {
        "status": status,
        "app_env": settings.app_env,
        "checks": checks,
    }, 200 if ok else 503


# --- CRON: execução automática do ciclo Cleiton (Render Cron Job / agendador externo) ---
@app.route("/cron/executar-cleiton", methods=["GET", "POST"])
def cron_executar_cleiton():
    """
    Rota para agendador (ex.: Render Cron Job). Respeita frequência e janela; não usa bypass.
    Protegida por CRON_SECRET: header X-Cron-Secret ou query ?secret=<CRON_SECRET>.
    """
    secret = request.headers.get("X-Cron-Secret") or request.args.get("secret")
    expected = settings.cron_secret
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
