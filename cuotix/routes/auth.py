import os
import secrets
import time
from datetime import timedelta

import requests
from firebase_admin import auth, firestore
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from ..extensions import firebase_app, get_db
from ..security import (
    bootstrap_rate_limited,
    local_credentials_configured,
    local_development_request,
    login_rate_limited,
    remote_local_admin_enabled,
)
from ..utils import active, required


bp = Blueprint('auth', __name__)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    configured = bool(os.environ.get('FIREBASE_WEB_API_KEY'))
    remote_local_configured = remote_local_admin_enabled()
    local_configured = (
        local_development_request() and local_credentials_configured()
        or remote_local_configured
    )
    can_login = configured or local_configured
    if request.method == 'POST':
        if local_configured:
            ip = request.remote_addr or 'unknown'
            if login_rate_limited(ip):
                flash('Demasiados intentos. Espera cinco minutos.', 'danger')
                return render_template(
                    'login.html',
                    page='login',
                    configured=configured,
                    local_configured=local_configured,
                    remote_local_configured=remote_local_configured,
                    can_login=can_login,
                ), 429
            expected_email = os.environ.get('LOCAL_ADMIN_EMAIL', '').strip().lower()
            expected_password = os.environ.get('LOCAL_ADMIN_PASSWORD', '')
            supplied_email = request.form.get('email', '').strip().lower()
            supplied_password = request.form.get('password', '')
            email_matches = secrets.compare_digest(
                supplied_email.encode('utf-8'), expected_email.encode('utf-8'))
            password_matches = secrets.compare_digest(
                supplied_password.encode('utf-8'), expected_password.encode('utf-8'))
            if email_matches and password_matches:
                session.clear()
                session.update(local_admin=True, last_active=time.time())
                session.permanent = True
                return redirect(url_for('dashboard.home'))
            flash('Correo o contraseña local incorrectos.', 'danger')
        elif not configured:
            flash('Configura FIREBASE_WEB_API_KEY para habilitar el inicio de sesión.', 'warning')
        else:
            ip = request.remote_addr or 'unknown'
            if login_rate_limited(ip):
                flash('Demasiados intentos. Espera cinco minutos.', 'danger')
                return render_template(
                    'login.html',
                    page='login',
                    configured=configured,
                    local_configured=local_configured,
                    remote_local_configured=remote_local_configured,
                    can_login=can_login,
                ), 429
            try:
                result = requests.post(
                    'https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword',
                    params={'key': os.environ['FIREBASE_WEB_API_KEY']},
                    timeout=15,
                    json={
                        'email': request.form.get('email', '').strip(),
                        'password': request.form.get('password', ''),
                        'returnSecureToken': True,
                    },
                )
                if result.status_code != 200:
                    raise ValueError('Correo o contraseña incorrectos, o cuenta deshabilitada.')
                token = auth.verify_id_token(result.json()['idToken'], check_revoked=True, app=firebase_app)
                profile = get_db().collection('usuarios').document(token['uid']).get().to_dict() or {}
                if str(profile.get('rol', '')).lower() not in ('admin', 'administrador') or not active(profile):
                    raise ValueError('Tu cuenta no tiene el rol de administrador activo.')
                cookie = auth.create_session_cookie(
                    result.json()['idToken'],
                    expires_in=timedelta(hours=8),
                    app=firebase_app,
                )
                session.clear()
                session.update(firebase_session=cookie, last_active=time.time())
                session.permanent = True
                return redirect(url_for('dashboard.home'))
            except ValueError as exc:
                flash(str(exc), 'danger')
            except Exception:
                current_app.logger.error('No se pudo completar la autenticación.')
                flash('No se pudo conectar con el servicio de acceso. Inténtalo nuevamente.', 'danger')
    return render_template(
        'login.html',
        page='login',
        configured=configured,
        local_configured=local_configured,
        remote_local_configured=remote_local_configured,
        can_login=can_login,
    )


@bp.route('/registro-admin', methods=['GET', 'POST'])
def register_admin():
    if not local_development_request():
        abort(404)
    configured = firebase_app is not None
    login_configured = bool(os.environ.get('FIREBASE_WEB_API_KEY'))
    local_login_configured = local_credentials_configured()

    if request.method == 'POST' and not configured:
        flash('Configura Firebase Admin en el servidor antes de crear la cuenta.', 'warning')
        return render_template(
            'registro_admin.html',
            page='register',
            configured=False,
            login_configured=login_configured,
            local_login_configured=local_login_configured,
        )

    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        if bootstrap_rate_limited(ip):
            flash('Demasiados intentos. Espera quince minutos.', 'danger')
            return render_template(
                'registro_admin.html',
                page='register',
                configured=configured,
                login_configured=login_configured,
                local_login_configured=local_login_configured,
            ), 429

        account = None
        try:
            first_name = required('nombres', 100)
            last_name = required('apellidos', 100)
            email = required('email').lower()
            password = request.form.get('password', '')
            password_confirm = request.form.get('password_confirm', '')
            if len(password) < 12 or len(password) > 128:
                raise ValueError('La contraseña debe tener entre 12 y 128 caracteres.')
            if password != password_confirm:
                raise ValueError('Las contraseñas no coinciden.')

            db = get_db()
            users_ref = db.collection('usuarios')
            bootstrap_ref = db.collection('configuracionSistema').document('adminBootstrap')
            if bootstrap_ref.get().exists:
                raise ValueError('El registro inicial ya fue utilizado.')
            if any(
                str((doc.to_dict() or {}).get('rol', '')).lower() in ('admin', 'administrador')
                for doc in users_ref.stream(timeout=20)
            ):
                raise ValueError('Ya existe un administrador. Usa su cuenta para iniciar sesión.')

            account = auth.create_user(
                email=email,
                password=password,
                display_name=first_name + ' ' + last_name,
                app=firebase_app,
            )
            profile_ref = users_ref.document(account.uid)
            profile = dict(
                nombres=first_name,
                apellidos=last_name,
                email=email,
                rol='admin',
                estado='activo',
                fecha_Creacion=firestore.SERVER_TIMESTAMP,
            )

            @firestore.transactional
            def save_first_admin(tx):
                if bootstrap_ref.get(transaction=tx).exists:
                    raise ValueError('El registro inicial ya fue utilizado.')
                tx.create(bootstrap_ref, dict(uid=account.uid, fecha=firestore.SERVER_TIMESTAMP))
                tx.create(profile_ref, profile)

            save_first_admin(db.transaction())
            if login_configured or local_login_configured:
                flash('Cuenta administradora creada. Ya puedes iniciar sesión.', 'success')
            else:
                flash(
                    'Cuenta creada. Para iniciar sesión, configura FIREBASE_WEB_API_KEY en .env y reinicia el servidor.',
                    'warning',
                )
            return redirect(url_for('auth.login'))
        except ValueError as exc:
            if account:
                try:
                    auth.delete_user(account.uid, app=firebase_app)
                except Exception as cleanup_error:
                    current_app.logger.error('No se pudo limpiar el alta incompleta: %s', type(cleanup_error).__name__)
            flash(str(exc), 'danger')
        except Exception as exc:
            if account:
                try:
                    auth.delete_user(account.uid, app=firebase_app)
                except Exception as cleanup_error:
                    current_app.logger.error('No se pudo limpiar el alta incompleta: %s', type(cleanup_error).__name__)
            current_app.logger.error('No se pudo completar el alta inicial: %s', type(exc).__name__)
            flash('No se pudo crear la cuenta. Verifica Firebase y que el correo no esté registrado.', 'danger')

    return render_template(
        'registro_admin.html',
        page='register',
        configured=configured,
        login_configured=login_configured,
        local_login_configured=local_login_configured,
    )


@bp.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
