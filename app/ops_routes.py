"""
Rotas operacionais: diagnóstico OAuth, auditoria de usuários, promote-admin,
reset de pautas e health check.
Todas protegidas por X-Ops-Token (exceto /health). Registrado como Blueprint em web.py.
"""
import os
import time
import logging
from flask import Blueprint, request, current_app
from sqlalchemy import text, func

from app.extensions import db
from app.models import User, Pauta
from app.infra import ensure_database_schema, ops_token_required

logger = logging.getLogger(__name__)

ops_bp = Blueprint('ops', __name__)

REDIRECT_URI_DEFAULT = 'http://127.0.0.1:5000/login/google/callback'


@ops_bp.route('/ops/reset-pautas', methods=['POST'])
def ops_reset_pautas():
    """Reseta pautas presas em 'em_processamento' de volta para 'pendente'."""
    ops_token_required()
    try:
        updated_count = Pauta.query.filter_by(status='em_processamento').update({'status': 'pendente'})
        db.session.commit()
        logger.info("Reset de pautas: %d pautas resetadas para 'pendente'.", updated_count)
        return {'status': 'ok', 'reset_count': updated_count}, 200
    except Exception as e:
        db.session.rollback()
        logger.exception("Falha ao resetar pautas: %s", e)
        return {'status': 'error', 'message': str(e)}, 500


@ops_bp.route('/oauth-diagnostics')
def oauth_diagnostics():
    """Diagnóstico do estado do OAuth. Requer header X-Ops-Token."""
    ops_token_required()
    try:
        logger.info("=== DIAGNÓSTICO OAUTH ===")
        redirect_uri = os.getenv('GOOGLE_OAUTH_REDIRECT_URI', REDIRECT_URI_DEFAULT)
        diagnostics = {
            "google_oauth_client_id": "✓ Configurado" if os.getenv('GOOGLE_OAUTH_CLIENT_ID') else "✗ NÃO CONFIGURADO",
            "google_oauth_client_secret": "✓ Configurado" if os.getenv('GOOGLE_OAUTH_CLIENT_SECRET') else "✗ NÃO CONFIGURADO",
            "flask_app_debug": current_app.debug,
            "oauth_fluxo": "manual (callback /login/google/callback)",
            "redirect_uri_esperado": redirect_uri,
            "request_host": request.host,
            "request_url_root": request.url_root,
        }
        for key, value in diagnostics.items():
            logger.info("%s: %s", key, value)
        return {
            "status": "ok",
            "diagnostics": diagnostics,
            "message": "Verifique o terminal/logs para detalhes completos",
        }, 200
    except Exception as e:
        logger.error("Erro no diagnóstico: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}, 500


@ops_bp.route('/ops/user-audit', methods=['POST'])
def ops_user_audit():
    """Audita usuários: duplicidades por e-mail e status admin. Requer X-Ops-Token."""
    ops_token_required()
    ensure_database_schema(db)
    try:
        users = User.query.order_by(User.id.asc()).all()
        duplicate_groups = (
            db.session.query(
                func.lower(User.email).label('email_norm'),
                func.count(User.id).label('cnt'),
            )
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
        logger.exception('Erro em /ops/user-audit: %s', e)
        return {'status': 'error', 'message': str(e)}, 500


@ops_bp.route('/ops/promote-admin', methods=['POST'])
def ops_promote_admin():
    """Promove usuário a admin pelo e-mail. Requer X-Ops-Token."""
    ops_token_required()
    ensure_database_schema(db)
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
                {'id': u.id, 'email': u.email, 'is_admin': bool(u.is_admin)}
                for u in users
            ],
        }, 200
    except Exception as e:
        db.session.rollback()
        logger.exception('Erro em /ops/promote-admin: %s', e)
        return {'status': 'error', 'message': str(e)}, 500


@ops_bp.route('/health')
def health_check():
    """Health check para monitoramento (Prod/Homolog)."""
    db_status = "unknown"
    try:
        db.session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = "error: %s" % str(e)
    return {
        "status": "ok",
        "ambiente": os.getenv('APP_ENV', 'dev'),
        "database": db_status,
        "timestamp": time.time(),
    }, 200
