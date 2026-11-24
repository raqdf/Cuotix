import os
import secrets
import time
from collections import defaultdict
from functools import wraps
from threading import Lock

from firebase_admin import auth
from flask import abort, current_app, g, redirect, request, session, url_for
from google.cloud.firestore_v1.base_query import FieldFilter

from .config import DENOMINATIONS, NAV, TITLES
from .extensions import firebase_app, get_db
from .utils import active, csrf_token, fullname, now


attempts = defaultdict(list)
bootstrap_attempts = defaultdict(list)
attempt_lock = Lock()


def local_development_request():
    host = request.host
    hostname = host[1:host.find(']')] if host.startswith('[') else host.split(':', 1)[0]
    return (current_app.debug
            and request.remote_addr in ('127.0.0.1', '::1')
            and hostname in ('localhost', '127.0.0.1', '::1')
            and not request.headers.get('Forwarded')
            and not request.headers.get('X-Forwarded-For')
            and not request.headers.get('X-Forwarded-Host'))


def local_credentials_configured():
    return bool(os.environ.get('LOCAL_ADMIN_EMAIL', '').strip()
                and len(os.environ.get('LOCAL_ADMIN_PASSWORD', '')) >= 12)


def remote_local_admin_enabled():
    password = os.environ.get('LOCAL_ADMIN_PASSWORD', '')
    return (os.environ.get('ENABLE_REMOTE_LOCAL_ADMIN') == '1'
            and not current_app.debug
            and not os.environ.get('FIREBASE_WEB_API_KEY', '').strip()
            and local_credentials_configured()
            and len(password) >= 16)


def login_rate_limited(ip):
    with attempt_lock:
        attempts[ip] = [t for t in attempts[ip] if time.time() - t < 300]
        limited = len(attempts[ip]) >= 8
        if not limited:
            attempts[ip].append(time.time())
    return limited


def bootstrap_rate_limited(ip):
    with attempt_lock:
        bootstrap_attempts[ip] = [t for t in bootstrap_attempts[ip] if time.time() - t < 900]
        limited = len(bootstrap_attempts[ip]) >= 5
        if not limited:
            bootstrap_attempts[ip].append(time.time())
    return limited


def admin_required(view):
    @wraps(view)
    def guarded(*args, **kwargs):
        if session.get('local_admin'):
            local_login_allowed = (local_development_request() and local_credentials_configured()
                                   or remote_local_admin_enabled())
            if not local_login_allowed or time.time() - session.get('last_active', 0) > 900:
                session.clear()
                return redirect(url_for('auth.login'))
            email = os.environ.get('LOCAL_ADMIN_EMAIL', '').strip().lower()
            admin_query = get_db().collection('usuarios').where(
                filter=FieldFilter('email', '==', email)).limit(1)
            admin_document = next(admin_query.stream(timeout=20), None)
            profile = admin_document.to_dict() if admin_document else {}
            if (str(profile.get('rol', '')).lower() not in ('admin', 'administrador')
                    or not active(profile)):
                session.clear()
                abort(403, 'No se encontró un perfil administrador activo para LOCAL_ADMIN_EMAIL en la base de datos.')
            name = (fullname(profile) if profile.get('nombres') else
                    profile.get('nombre') or profile.get('email') or 'Administrador')
            g.admin = dict(profile, id=admin_document.id, nombre=name)
            session['last_active'] = time.time()
            return view(*args, **kwargs)

        cookie = session.get('firebase_session')
        if not cookie or time.time() - session.get('last_active', 0) > 900:
            session.clear()
            return redirect(url_for('auth.login'))
        try:
            token = auth.verify_session_cookie(cookie, check_revoked=True, app=firebase_app)
        except (auth.InvalidSessionCookieError, auth.ExpiredSessionCookieError,
                auth.RevokedSessionCookieError, auth.UserDisabledError):
            session.clear()
            return redirect(url_for('auth.login'))
        profile = get_db().collection('usuarios').document(token['uid']).get().to_dict() or {}
        if str(profile.get('rol', '')).lower() not in ('admin', 'administrador') or not active(profile):
            session.clear()
            abort(403, 'Esta cuenta no tiene acceso administrativo.')
        g.admin = dict(profile, id=token['uid'], nombre=fullname(profile) if profile.get('nombres') else 'Administrador')
        session['last_active'] = time.time()
        return view(*args, **kwargs)
    return guarded


def register_security_hooks(app):
    @app.context_processor
    def common():
        return dict(
            csrf_token=csrf_token,
            nav=NAV,
            titles=TITLES,
            today=now().date().isoformat(),
            denominations=DENOMINATIONS,
        )

    @app.before_request
    def csrf_check():
        if request.method == 'POST':
            expected = session.get('csrf', '')
            if not expected or not secrets.compare_digest(expected, request.form.get('csrf_token', '')):
                abort(400, 'El formulario venció. Recarga la página e inténtalo otra vez.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response
