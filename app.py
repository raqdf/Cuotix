import csv
import io
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from threading import Lock

import requests
from firebase_admin import auth, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, Response
from werkzeug.exceptions import HTTPException
from firebase_config import firebase_app, get_db

app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE') == '1',
    MAX_CONTENT_LENGTH=64 * 1024, PERMANENT_SESSION_LIFETIME=timedelta(minutes=15))
LOCAL_TZ = timezone(timedelta(hours=-6))
COLLECTIONS = ('usuarios', 'clientes', 'creditos', 'cuotas', 'facturas', 'movimientos')
NAV = [('monitor', '/', 'grid', 'Monitoreo & Cobros'), ('cobradores', '/cobradores', 'people', 'Gestión de Cobradores'),
       ('caja', '/caja', 'wallet', 'Inventario de dinero'), ('clientes', '/clientes', 'folder', 'Historial & Clientes'),
       ('reportes', '/reportes', 'chart', 'Reportes & Métricas'), ('apariencia', '/apariencia', 'palette', 'Paletas de color')]
TITLES = {'monitor': 'Centro de Monitoreo & Operaciones de Cobro', 'cobradores': 'Control Maestro de Accesos & Roles',
          'caja': 'Inventario de dinero', 'clientes': 'Historial Crediticio por Cliente',
          'reportes': 'Inteligencia Financiera, Reportes y Rendimiento de Cartera', 'auditoria': 'Registro de auditoría',
          'apariencia': 'Paletas de color'}
DENOMINATIONS = (1000, 500, 200, 100, 50, 20, 10)
attempts = defaultdict(list)
bootstrap_attempts = defaultdict(list)
attempt_lock = Lock()


def now():
    return datetime.now(LOCAL_TZ)


def number(value):
    try:
        result = Decimal(str(value or 0).replace(',', '').replace('%', '').strip())
        return result if result.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError):
        return Decimal(0)


def amount(value):
    try:
        result = Decimal(str(value)).quantize(Decimal('.01'))
        if not result.is_finite() or result < 0 or result > Decimal('1000000000'):
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ValueError('Ingresa un monto válido entre 0 y 1,000,000,000.')


@app.template_filter('money')
def money(value):
    return f'C$ {number(value):,.2f}'


def date_value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ) if value.tzinfo is None else value.astimezone(LOCAL_TZ)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed.astimezone(LOCAL_TZ)
    except (ValueError, TypeError):
        return None


@app.template_filter('datefmt')
def datefmt(value, fmt='%d/%m/%Y %H:%M'):
    dt = date_value(value)
    return dt.strftime(fmt) if dt else 'Sin fecha'


def ref_id(value):
    return value.id if hasattr(value, 'id') else str(value or '').rstrip('/').split('/')[-1]


def active(user):
    return str(user.get('estado', 'activo')).lower() in ('activo', 'true', '1')


def fullname(user):
    return ' '.join(str(user.get(k, '')).strip() for k in ('nombres', 'apellidos')).strip() or user.get('email') or user.get('id', 'Sin nombre')


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


@app.context_processor
def common():
    return dict(csrf_token=csrf_token, nav=NAV, titles=TITLES, today=now().date().isoformat(), denominations=DENOMINATIONS)


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


def local_development_request():
    host = request.host
    hostname = host[1:host.find(']')] if host.startswith('[') else host.split(':', 1)[0]
    return (app.debug
            and request.remote_addr in ('127.0.0.1', '::1')
            and hostname in ('localhost', '127.0.0.1', '::1')
            and not request.headers.get('Forwarded')
            and not request.headers.get('X-Forwarded-For')
            and not request.headers.get('X-Forwarded-Host'))


def local_credentials_configured():
    return bool(os.environ.get('LOCAL_ADMIN_EMAIL', '').strip()
                and len(os.environ.get('LOCAL_ADMIN_PASSWORD', '')) >= 12)


def login_rate_limited(ip):
    with attempt_lock:
        attempts[ip] = [t for t in attempts[ip] if time.time() - t < 300]
        limited = len(attempts[ip]) >= 8
        if not limited:
            attempts[ip].append(time.time())
    return limited


def admin_required(view):
    @wraps(view)
    def guarded(*args, **kwargs):
        if session.get('local_admin'):
            if not local_development_request() or time.time() - session.get('last_active', 0) > 900:
                session.clear()
                return redirect(url_for('login'))
            g.admin = {'id': 'local-admin', 'nombre': 'Administrador local', 'rol': 'Admin'}
            session['last_active'] = time.time()
            return view(*args, **kwargs)
        cookie = session.get('firebase_session')
        if not cookie or time.time() - session.get('last_active', 0) > 900:
            session.clear()
            return redirect(url_for('login'))
        try:
            token = auth.verify_session_cookie(cookie, check_revoked=True, app=firebase_app)
        except (auth.InvalidSessionCookieError, auth.ExpiredSessionCookieError, auth.RevokedSessionCookieError, auth.UserDisabledError):
            session.clear()
            return redirect(url_for('login'))
        profile = get_db().collection('usuarios').document(token['uid']).get().to_dict() or {}
        if str(profile.get('rol', '')).lower() not in ('admin', 'administrador') or not active(profile):
            session.clear()
            abort(403, 'Esta cuenta no tiene acceso administrativo.')
        g.admin = dict(profile, id=token['uid'], nombre=fullname(profile) if profile.get('nombres') else 'Administrador')
        session['last_active'] = time.time()
        return view(*args, **kwargs)
    return guarded


@app.route('/login', methods=['GET', 'POST'])
def login():
    configured = bool(os.environ.get('FIREBASE_WEB_API_KEY'))
    local_configured = local_development_request() and local_credentials_configured()
    can_login = configured or local_configured
    if request.method == 'POST':
        if local_configured:
            ip = request.remote_addr or 'unknown'
            if login_rate_limited(ip):
                flash('Demasiados intentos. Espera cinco minutos.', 'danger')
                return render_template('login.html', page='login', configured=configured,
                                       local_configured=local_configured, can_login=can_login), 429
            expected_email = os.environ.get('LOCAL_ADMIN_EMAIL', '').strip().lower()
            expected_password = os.environ.get('LOCAL_ADMIN_PASSWORD', '')
            supplied_email = request.form.get('email', '').strip().lower()
            supplied_password = request.form.get('password', '')
            email_matches = secrets.compare_digest(supplied_email.encode('utf-8'), expected_email.encode('utf-8'))
            password_matches = secrets.compare_digest(supplied_password.encode('utf-8'), expected_password.encode('utf-8'))
            if email_matches and password_matches:
                session.clear()
                session.update(local_admin=True, last_active=time.time())
                session.permanent = True
                return redirect(url_for('home'))
            flash('Correo o contraseña local incorrectos.', 'danger')
        elif not configured:
            flash('Configura FIREBASE_WEB_API_KEY para habilitar el inicio de sesión.', 'warning')
        else:
            ip = request.remote_addr or 'unknown'
            if login_rate_limited(ip):
                flash('Demasiados intentos. Espera cinco minutos.', 'danger')
                return render_template('login.html', page='login', configured=configured,
                                       local_configured=local_configured, can_login=can_login), 429
            try:
                result = requests.post('https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword',
                    params={'key': os.environ['FIREBASE_WEB_API_KEY']}, timeout=15,
                    json={'email': request.form.get('email', '').strip(), 'password': request.form.get('password', ''), 'returnSecureToken': True})
                if result.status_code != 200:
                    raise ValueError('Correo o contraseña incorrectos, o cuenta deshabilitada.')
                token = auth.verify_id_token(result.json()['idToken'], check_revoked=True, app=firebase_app)
                profile = get_db().collection('usuarios').document(token['uid']).get().to_dict() or {}
                if str(profile.get('rol', '')).lower() not in ('admin', 'administrador') or not active(profile):
                    raise ValueError('Tu cuenta no tiene el rol de administrador activo.')
                cookie = auth.create_session_cookie(result.json()['idToken'], expires_in=timedelta(hours=8), app=firebase_app)
                session.clear()
                session.update(firebase_session=cookie, last_active=time.time())
                session.permanent = True
                return redirect(url_for('home'))
            except ValueError as exc:
                flash(str(exc), 'danger')
            except Exception:
                app.logger.error('No se pudo completar la autenticación.')
                flash('No se pudo conectar con el servicio de acceso. Inténtalo nuevamente.', 'danger')
    return render_template('login.html', page='login', configured=configured,
                           local_configured=local_configured, can_login=can_login)


@app.route('/registro-admin', methods=['GET', 'POST'])
def register_admin():
    if not local_development_request():
        abort(404)
    configured = firebase_app is not None
    login_configured = bool(os.environ.get('FIREBASE_WEB_API_KEY'))
    local_login_configured = local_credentials_configured()

    if request.method == 'POST' and not configured:
        flash('Configura Firebase Admin en el servidor antes de crear la cuenta.', 'warning')
        return render_template('registro_admin.html', page='register', configured=False,
                               login_configured=login_configured, local_login_configured=local_login_configured)

    if request.method == 'POST':
        ip = request.remote_addr or 'unknown'
        with attempt_lock:
            bootstrap_attempts[ip] = [t for t in bootstrap_attempts[ip] if time.time() - t < 900]
            limited = len(bootstrap_attempts[ip]) >= 5
            if not limited:
                bootstrap_attempts[ip].append(time.time())
        if limited:
            flash('Demasiados intentos. Espera quince minutos.', 'danger')
            return render_template('registro_admin.html', page='register', configured=configured,
                                   login_configured=login_configured,
                                   local_login_configured=local_login_configured), 429

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
            if any(str((doc.to_dict() or {}).get('rol', '')).lower() in ('admin', 'administrador')
                   for doc in users_ref.stream(timeout=20)):
                raise ValueError('Ya existe un administrador. Usa su cuenta para iniciar sesión.')

            account = auth.create_user(email=email, password=password,
                                       display_name=first_name + ' ' + last_name, app=firebase_app)
            profile_ref = users_ref.document(account.uid)
            profile = dict(nombres=first_name, apellidos=last_name, email=email, rol='admin',
                           estado='activo', fecha_Creacion=firestore.SERVER_TIMESTAMP)

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
                flash('Cuenta creada. Para iniciar sesión, configura FIREBASE_WEB_API_KEY en .env y reinicia el servidor.', 'warning')
            return redirect(url_for('login'))
        except ValueError as exc:
            if account:
                try:
                    auth.delete_user(account.uid, app=firebase_app)
                except Exception as cleanup_error:
                    app.logger.error('No se pudo limpiar el alta incompleta: %s', type(cleanup_error).__name__)
            flash(str(exc), 'danger')
        except Exception as exc:
            if account:
                try:
                    auth.delete_user(account.uid, app=firebase_app)
                except Exception as cleanup_error:
                    app.logger.error('No se pudo limpiar el alta incompleta: %s', type(cleanup_error).__name__)
            app.logger.error('No se pudo completar el alta inicial: %s', type(exc).__name__)
            flash('No se pudo crear la cuenta. Verifica Firebase y que el correo no esté registrado.', 'danger')

    return render_template('registro_admin.html', page='register', configured=configured,
                           login_configured=login_configured, local_login_configured=local_login_configured)


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


def read_collection(name):
    return [dict(doc.to_dict(), id=doc.id) for doc in get_db().collection(name).stream(timeout=20)]


def load_data():
    data = {name: read_collection(name) for name in COLLECTIONS}
    users = {u['id']: u for u in data['usuarios']}
    clients = {c['id']: c for c in data['clientes']}
    credits = {c['id']: c for c in data['creditos']}
    for u in data['usuarios']:
        u.update(nombre=fullname(u), activo=active(u))
        u['cartera'] = sum((number(c.get('saldoPendiente')) for c in data['creditos'] if ref_id(c.get('cobradorId')) == u['id']), Decimal(0))
        u['contratos'] = sum(1 for c in data['creditos'] if ref_id(c.get('cobradorId')) == u['id'] and number(c.get('saldoPendiente')) > 0)
    for c in data['clientes']:
        c.update(nombre=fullname(c), cobrador=users.get(ref_id(c.get('cobradorId')), {}).get('nombre', 'Sin asignar'))
    for collection in ('creditos', 'cuotas', 'facturas', 'movimientos'):
        for row in data[collection]:
            if collection == 'cuotas':
                row['numeroCuota'] = int(number(row.get('numeroCuota')))
            credit = credits.get(ref_id(row.get('creditoId')), {})
            client_id = ref_id(row.get('clienteId') or credit.get('clienteId'))
            collector_id = ref_id(row.get('cobradorId') or row.get('registradoPor') or credit.get('cobradorId'))
            row.update(cliente_id=client_id, cobrador_id=collector_id,
                cliente=clients.get(client_id, {}).get('nombre') or row.get('clienteNombre') or row.get('nombreCliente', 'Sin vincular'),
                cobrador=users.get(collector_id, {}).get('nombre') or row.get('cobradorNombre') or row.get('nombreCobrador', 'Sin vincular'))
    data['cobradores'] = [u for u in data['usuarios'] if str(u.get('rol', '')).lower() == 'cobrador']
    return data


def period():
    try:
        start = datetime.strptime(request.args.get('desde') or now().date().replace(day=1).isoformat(), '%Y-%m-%d').date()
        end = datetime.strptime(request.args.get('hasta') or now().date().isoformat(), '%Y-%m-%d').date()
        if start > end:
            raise ValueError()
        return start, end
    except ValueError:
        abort(400, 'El rango de fechas no es válido.')


def confirmed(row):
    return str(row.get('estado', '')).lower() not in ('anulado', 'reversado', 'cancelado')


def receipt_amount(row):
    return number(row.get('monto', row.get('totalTransaccion', row.get('abonoAplicado', 0))))


def build_context(page, data):
    start, end = period()
    collector = request.args.get('cobrador', '')
    q = request.args.get('q', '').strip().casefold()
    receipts = [r for r in data['facturas'] if confirmed(r)]
    filtered = [r for r in receipts if date_value(r.get('fecha')) and start <= date_value(r['fecha']).date() <= end and (not collector or r['cobrador_id'] == collector)]
    filtered.sort(key=lambda r: date_value(r['fecha']), reverse=True)
    today_receipts = [r for r in receipts if date_value(r.get('fecha')) and date_value(r['fecha']).date() == now().date() and (not collector or r['cobrador_id'] == collector)]
    credits = [c for c in data['creditos'] if not collector or c['cobrador_id'] == collector]
    overdue = [c for c in data['cuotas'] if number(c.get('saldoPendiente')) > 0 and date_value(c.get('fechaVencimiento')) and date_value(c['fechaVencimiento']).date() < now().date() and (not collector or c['cobrador_id'] == collector)]
    portfolio = sum((number(c.get('saldoPendiente')) for c in credits), Decimal(0))
    overdue_total = sum((number(c.get('saldoPendiente')) for c in overdue), Decimal(0))
    stats = dict(collected=sum((receipt_amount(r) for r in filtered), Decimal(0)), today=sum((receipt_amount(r) for r in today_receipts), Decimal(0)),
        count=len(filtered), today_count=len(today_receipts), portfolio=portfolio, overdue=overdue_total, overdue_count=len(overdue),
        overdue_rate=float(overdue_total / portfolio * 100) if portfolio else 0, active_credits=sum(number(c.get('saldoPendiente')) > 0 for c in credits),
        placed=sum((number(c.get('monto')) for c in credits if date_value(c.get('fechaCreacion')) and start <= date_value(c['fechaCreacion']).date() <= end), Decimal(0)))
    for u in data['cobradores']:
        u['cobrado'] = sum((receipt_amount(r) for r in filtered if r['cobrador_id'] == u['id']), Decimal(0))
        u['hoy'] = sum((receipt_amount(r) for r in today_receipts if r['cobrador_id'] == u['id']), Decimal(0))
        u['avance'] = min(100, float(u['hoy'] / number(u.get('metaDiaria')) * 100)) if number(u.get('metaDiaria')) else 0
    visible_receipts = [r for r in filtered if not q or q in ' '.join(str(r.get(k, '')) for k in ('cliente', 'cobrador', 'numeroFactura', 'creditoCodigo', 'id')).casefold()]
    visible_users = [u for u in data['usuarios'] if not q or q in (u['nombre'] + ' ' + str(u.get('cedula', '')) + ' ' + u['id']).casefold()]
    state = request.args.get('estado', '')
    if state == 'activo':
        visible_users = [u for u in visible_users if u['activo']]
    elif state == 'inactivo':
        visible_users = [u for u in visible_users if not u['activo'] and str(u.get('estado', '')).lower() != 'baneado']
    elif state == 'baneado':
        visible_users = [u for u in visible_users if str(u.get('estado', '')).lower() == 'baneado']
    elif state:
        visible_users = []
    client_list = [c for c in data['clientes'] if not q or q in (c['nombre'] + ' ' + str(c.get('cedula', '')) + ' ' + c['id']).casefold()]
    selected = next((c for c in client_list if c['id'] == request.args.get('cliente')), client_list[0] if client_list else None)
    client_credits = [c for c in data['creditos'] if selected and c['cliente_id'] == selected['id']]
    client_receipts = [r for r in receipts if selected and r['cliente_id'] == selected['id']]
    client_receipts.sort(key=lambda r: date_value(r.get('fecha')) or datetime.min.replace(tzinfo=LOCAL_TZ), reverse=True)
    cash = (get_db().collection('cajaAdmin').document('central').get().to_dict() or {}) if page == 'caja' else {}
    cash_history = read_collection('movimientosCaja') if page == 'caja' else []
    cash_history.sort(key=lambda r: date_value(r.get('fecha')) or datetime.min.replace(tzinfo=LOCAL_TZ), reverse=True)
    pending = [r for r in receipts if str(r.get('metodoPago', r.get('tipoPago', ''))).lower() == 'efectivo' and not r.get('liquidacionAdminId')]
    for u in data['cobradores']:
        u['pendienteCaja'] = sum((receipt_amount(r) for r in pending if r['cobrador_id'] == u['id']), Decimal(0))
    trend = []
    for offset in range(6, -1, -1):
        day = now().date() - timedelta(days=offset)
        value = sum((receipt_amount(r) for r in receipts if date_value(r.get('fecha')) and date_value(r['fecha']).date() == day and (not collector or r['cobrador_id'] == collector)), Decimal(0))
        trend.append({'label': day.strftime('%d/%m'), 'value': float(value)})
    return dict(page=page, data=data, stats=stats, receipts=visible_receipts, users=visible_users, clients=client_list,
        selected=selected, client_credits=client_credits, client_receipts=client_receipts, cash=cash, cash_history=cash_history[:30],
        inventory_history=[r for r in cash_history if r.get('tipo') == 'inventario'],
        pending_cash=sum((receipt_amount(r) for r in pending), Decimal(0)), trend=trend, q=request.args.get('q', ''), start=start, end=end,
        collector=collector, active_collectors=[u for u in data['cobradores'] if u['activo']], updated=now(), admin=g.admin)


def page_response(page):
    templates = {
        'monitor': 'index.html',
        'cobradores': 'cobradores.html',
        'caja': 'caja.html',
        'clientes': 'clientes.html',
        'reportes': 'reportes.html',
        'apariencia': 'apariencia.html',
    }
    return render_template(templates[page], **build_context(page, load_data()))


@app.get('/')
@admin_required
def home():
    return page_response('monitor')


@app.get('/cobradores', defaults={'page': 'cobradores'})
@app.get('/caja', defaults={'page': 'caja'})
@app.get('/clientes', defaults={'page': 'clientes'})
@app.get('/reportes', defaults={'page': 'reportes'})
@app.get('/apariencia', defaults={'page': 'apariencia'})
@admin_required
def panel(page):
    return page_response(page)


def required(name, limit=150):
    value = request.form.get(name, '').strip()
    if not value or len(value) > limit:
        raise ValueError(f'Completa correctamente el campo {name}.')
    return value


def valid_id(value):
    if not value or '/' in value or len(value) > 128:
        raise ValueError('Identificador inválido.')
    return value


def audit(tx, action, details):
    tx.set(get_db().collection('auditoriaAdmin').document(), dict(accion=action, detalle=details, adminId=g.admin['id'], fecha=firestore.SERVER_TIMESTAMP))


@app.post('/cobradores/crear')
@admin_required
def create_collector():
    account = None
    try:
        fields = {k: required(k) for k in ('nombres', 'apellidos', 'cedula', 'numero', 'email')}
        password = required('password', 128)
        if len(password) < 8:
            raise ValueError('La contraseña temporal debe tener al menos 8 caracteres.')
        goal = amount(request.form.get('metaDiaria', '0'))
        account = auth.create_user(email=fields['email'], password=password, disabled=True, display_name=fields['nombres'] + ' ' + fields['apellidos'], app=firebase_app)
        batch = get_db().batch()
        batch.set(get_db().collection('usuarios').document(account.uid), dict(fields, rol='cobrador', estado='pendiente', estadoSolicitado='activo', metaDiaria=float(goal), ruta=request.form.get('ruta', '').strip()[:100], fecha_Creacion=firestore.SERVER_TIMESTAMP))
        audit(batch, 'crear_cobrador', {'uid': account.uid})
        batch.commit()
        auth.update_user(account.uid, disabled=False, app=firebase_app)
        activation = get_db().batch()
        activation.update(get_db().collection('usuarios').document(account.uid), {'estado': 'activo', 'estadoSolicitado': firestore.DELETE_FIELD})
        audit(activation, 'activar_nuevo_cobrador', {'uid': account.uid})
        activation.commit()
        flash('Cobrador creado. Puede ingresar con su correo y contraseña temporal.', 'success')
    except Exception as exc:
        if account:
            flash('El alta quedó pendiente. Si el cobrador aparece en la lista, usa Acceso → Reactivar. Si no aparece, revisa su cuenta en Firebase Authentication antes de repetir el alta.', 'warning')
        else:
            flash(str(exc) if isinstance(exc, ValueError) else 'No se pudo crear el cobrador. Verifica el correo y la conexión.', 'danger')
    return redirect(url_for('panel', page='cobradores'))


@app.post('/cobradores/<uid>/editar')
@admin_required
def edit_collector(uid):
    try:
        ref = get_db().collection('usuarios').document(valid_id(uid))
        fields = {k: required(k) for k in ('nombres', 'apellidos', 'cedula', 'numero')}
        fields.update(ruta=request.form.get('ruta', '').strip()[:100], metaDiaria=float(amount(request.form.get('metaDiaria', '0'))))
        @firestore.transactional
        def save(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if str(profile.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Solo se puede editar un cobrador desde este formulario.')
            tx.update(ref, dict(fields, fechaActualizacion=firestore.SERVER_TIMESTAMP))
            audit(tx, 'editar_cobrador', {'uid': uid, 'campos': list(fields)})
        save(get_db().transaction())
        flash('Datos del cobrador actualizados.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='cobradores'))


@app.post('/cobradores/<uid>/estado')
@admin_required
def collector_status(uid):
    try:
        valid_id(uid)
        state, reason = required('estado'), required('motivo', 500)
        if state not in ('activo', 'deshabilitado', 'baneado') or uid == g.admin['id']:
            raise ValueError('Acción no permitida.')
        ref = get_db().collection('usuarios').document(uid)
        target_id = request.form.get('destino', '').strip()
        target = get_db().collection('usuarios').document(valid_id(target_id)) if target_id else None
        # Auth and Firestore cannot share a transaction. A pending state allows retry.
        @firestore.transactional
        def transfer(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if str(profile.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Solo puedes cambiar el estado de un cobrador.')
            docs = []
            if state != 'activo':
                for name in ('clientes', 'creditos', 'cuotas'):
                    docs.extend(get_db().collection(name).where(filter=FieldFilter('cobradorId', '==', ref)).stream(transaction=tx))
                if docs and (not target or target_id == uid):
                    raise ValueError('Este cobrador tiene cartera asignada. Selecciona otro cobrador activo; si no hay ninguno, créalo o reactívalo antes de cambiar el estado.')
                if target:
                    dest = target.get(transaction=tx).to_dict() or {}
                    if str(dest.get('rol', '')).lower() != 'cobrador' or not active(dest) or target_id == uid:
                        raise ValueError('El destino debe ser otro cobrador activo.')
                if len(docs) > 450:
                    raise ValueError('La cartera requiere una migración por lotes (más de 450 documentos).')
            for doc in docs:
                tx.update(doc.reference, {'cobradorId': target, 'fechaActualizacion': firestore.SERVER_TIMESTAMP})
            tx.update(ref, {'estado': 'pendiente', 'estadoSolicitado': state, 'motivoEstado': reason, 'fechaActualizacion': firestore.SERVER_TIMESTAMP})
            audit(tx, 'solicitar_estado_cobrador', {'uid': uid, 'estado': state, 'motivo': reason, 'destino': target_id, 'documentos': len(docs)})
        transfer(get_db().transaction())
        try:
            auth.update_user(uid, disabled=state != 'activo', app=firebase_app)
            auth.revoke_refresh_tokens(uid, app=firebase_app)
            batch = get_db().batch()
            batch.update(ref, {'estado': state, 'estadoSolicitado': firestore.DELETE_FIELD})
            audit(batch, 'confirmar_estado_cobrador', {'uid': uid, 'estado': state})
            batch.commit()
            flash('Estado actualizado y sesiones revocadas en Firebase Authentication.', 'success')
        except Exception:
            flash('Cartera procesada; sincronización de acceso pendiente. Repite el cambio de estado para completarla.', 'warning')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='cobradores'))


@app.post('/clientes/<client_id>/cobrador')
@admin_required
def assign_client_collector(client_id):
    try:
        client_id = valid_id(client_id)
        collector_id = valid_id(required('cobrador', 128))
        db = get_db()
        client_ref = db.collection('clientes').document(client_id)
        collector_ref = db.collection('usuarios').document(collector_id)

        @firestore.transactional
        def assign(tx):
            client = client_ref.get(transaction=tx)
            if not client.exists:
                raise ValueError('No se encontró el cliente.')
            collector_snapshot = collector_ref.get(transaction=tx)
            collector = collector_snapshot.to_dict() or {}
            if str(collector.get('rol', '')).lower() != 'cobrador' or not active(collector):
                raise ValueError('El destino debe ser un cobrador activo.')

            def related_documents(collection, field, values):
                documents = {}
                for value in values:
                    query = db.collection(collection).where(filter=FieldFilter(field, '==', value))
                    documents.update({doc.reference.path: doc for doc in query.stream(transaction=tx)})
                return documents

            client_values = (client_ref, client_id, client_ref.path)
            credits = related_documents('creditos', 'clienteId', client_values)
            credit_values = [
                value
                for doc in credits.values()
                for value in (doc.reference, doc.id, doc.reference.path)
            ]
            quotas = related_documents('cuotas', 'clienteId', client_values)
            quotas.update(related_documents('cuotas', 'creditoId', credit_values))
            if 1 + len(credits) + len(quotas) + 1 > 450:
                raise ValueError('La cartera requiere una reasignación por lotes (más de 450 documentos).')

            timestamp = firestore.SERVER_TIMESTAMP
            tx.update(client_ref, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            for doc in credits.values():
                tx.update(doc.reference, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            for doc in quotas.values():
                tx.update(doc.reference, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            audit(tx, 'asignar_cliente_cobrador', {
                'clienteId': client_id, 'cobradorId': collector_id,
                'creditos': len(credits), 'cuotas': len(quotas),
            })
            return fullname(collector), len(credits), len(quotas)

        collector_name, credit_count, quota_count = assign(db.transaction())
        flash(f'Cliente asignado a {collector_name}. Se actualizaron {credit_count} créditos y {quota_count} cuotas; los recibos históricos no cambiaron.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='clientes', cliente=client_id))


@app.get('/auditoria')
@admin_required
def audit_page():
    records = [dict(d.to_dict(), id=d.id) for d in get_db().collection('auditoriaAdmin').order_by('fecha', direction=firestore.Query.DESCENDING).limit(100).stream()]
    return render_template('auditoria.html', page='auditoria', records=records, admin=g.admin)


def physical_cash_count():
    counts = {}
    for denomination in DENOMINATIONS:
        raw = request.form.get(f'billete_{denomination}', '0')
        if not raw.isascii() or not raw.isdigit() or len(raw) > 7 or int(raw) > 1000000:
            raise ValueError('Las cantidades de billetes deben ser enteros entre 0 y 1,000,000.')
        counts[str(denomination)] = int(raw)
    coins = amount(request.form.get('monedas', '0'))
    total = sum((Decimal(k) * v for k, v in counts.items()), Decimal(0)) + coins
    return counts, coins, total


@app.post('/caja/inventario')
@admin_required
def save_cash_inventory():
    try:
        op = valid_id(required('operacion'))
        counts, coins, total = physical_cash_count()
        reason = request.form.get('motivo', '').strip()[:500]
        db = get_db()
        cash_ref = db.collection('cajaAdmin').document('central')
        log_ref = db.collection('movimientosCaja').document(op)

        @firestore.transactional
        def save(tx):
            cash = cash_ref.get(transaction=tx).to_dict() or {}
            if log_ref.get(transaction=tx).exists:
                return
            balance = number(cash.get('saldoCaja'))
            difference = total - balance
            if difference and not reason:
                raise ValueError('Registra el motivo de la diferencia antes de guardar el inventario.')
            tx.set(log_ref, {
                'tipo': 'inventario', 'monto': float(total), 'saldoAnterior': float(balance),
                'diferencia': float(difference), 'adminId': g.admin['id'],
                'adminNombre': g.admin.get('nombre', g.admin['id']),
                'fecha': firestore.SERVER_TIMESTAMP,
                'detalle': {'billetes': counts, 'monedas': float(coins), 'observacion': reason,
                            'estadoCaja': cash.get('estado', 'sin apertura')},
            })
            audit(tx, 'caja_inventario', {'operacion': op, 'monto': float(total)})

        save(db.transaction())
        flash('Inventario guardado. Puedes consultar el desglose en el historial de dinero inventariado.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='caja') + '#inventoryHistory')


@app.post('/caja/operacion')
@admin_required
def cash_operation():
    try:
        action = required('accion')
        op = valid_id(required('operacion'))
        ref = get_db().collection('cajaAdmin').document('central')
        log = get_db().collection('movimientosCaja').document(op)
        @firestore.transactional
        def operate(tx):
            cash = ref.get(transaction=tx).to_dict() or {}
            if log.get(transaction=tx).exists:
                return
            current, vault = number(cash.get('saldoCaja')), number(cash.get('saldoBoveda'))
            if action == 'apertura':
                if cash.get('estado') == 'abierta':
                    raise ValueError('La caja ya está abierta.')
                value = amount(required('monto'))
                if cash and value != current:
                    raise ValueError('La apertura debe coincidir con el último saldo de caja.')
                extra = {'estado': 'abierta', 'saldoCaja': float(value), 'saldoBoveda': float(vault), 'fechaApertura': firestore.SERVER_TIMESTAMP}
            else:
                if cash.get('estado') != 'abierta':
                    raise ValueError('Primero debes abrir la caja.')
                if action == 'transferencia':
                    value = amount(required('monto'))
                    if not 0 < value <= current:
                        raise ValueError('El monto debe ser positivo y no superar el saldo de caja.')
                    extra = {'saldoCaja': float(current - value), 'saldoBoveda': float(vault + value)}
                elif action == 'cierre':
                    counts, coins, value = physical_cash_count()
                    difference = value - current
                    reason = request.form.get('motivo', '').strip()[:500]
                    if difference and not reason:
                        raise ValueError('Registra el motivo de la diferencia antes de cerrar.')
                    extra = {'estado': 'cerrada', 'saldoCaja': float(value), 'ultimoConteo': counts, 'diferencia': float(difference), 'monedas': float(coins), 'fechaCierre': firestore.SERVER_TIMESTAMP, 'observacion': reason}
                else:
                    raise ValueError('Operación de caja desconocida.')
            tx.set(ref, dict(extra, fechaActualizacion=firestore.SERVER_TIMESTAMP), merge=True)
            tx.set(log, dict(tipo=action, monto=float(value), saldoAnterior=float(current), adminId=g.admin['id'], fecha=firestore.SERVER_TIMESTAMP, detalle=extra))
            audit(tx, 'caja_' + action, {'operacion': op, 'monto': float(value)})
        operate(get_db().transaction())
        flash('Operación de caja registrada.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='caja'))


@app.post('/caja/liquidar')
@admin_required
def settle():
    try:
        uid = valid_id(required('cobrador'))
        delivered = amount(required('monto'))
        note = request.form.get('motivo', '').strip()[:500]
        op = valid_id(required('operacion'))
        db = get_db()
        cash_ref, log_ref = db.collection('cajaAdmin').document('central'), db.collection('movimientosCaja').document(op)
        @firestore.transactional
        def save(tx):
            cash = cash_ref.get(transaction=tx).to_dict() or {}
            if log_ref.get(transaction=tx).exists:
                return
            if cash.get('estado') != 'abierta':
                raise ValueError('Abre la caja antes de recibir una liquidación.')
            user = db.collection('usuarios').document(uid).get(transaction=tx).to_dict() or {}
            if str(user.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Cobrador no válido.')
            invoices = list(db.collection('facturas').stream(transaction=tx))
            credits = {d.id: d.to_dict() for d in db.collection('creditos').stream(transaction=tx)}
            selected = []
            for doc in invoices:
                r = doc.to_dict()
                collector_id = ref_id(r.get('cobradorId') or credits.get(ref_id(r.get('creditoId')), {}).get('cobradorId'))
                if collector_id == uid and confirmed(r) and str(r.get('metodoPago', r.get('tipoPago', ''))).lower() == 'efectivo' and not r.get('liquidacionAdminId'):
                    selected.append(doc)
            if not selected:
                raise ValueError('No hay recibos de efectivo pendientes para este cobrador.')
            if len(selected) > 440:
                raise ValueError('Se requiere una liquidación por lotes (más de 440 recibos).')
            expected = sum((receipt_amount(d.to_dict()) for d in selected), Decimal(0))
            if delivered != expected and not note:
                raise ValueError('Explica la diferencia entre lo reportado y lo entregado.')
            tx.update(cash_ref, {'saldoCaja': float(number(cash.get('saldoCaja')) + delivered), 'fechaActualizacion': firestore.SERVER_TIMESTAMP})
            for d in selected:
                tx.update(d.reference, {'liquidacionAdminId': op})
            tx.set(log_ref, {'tipo': 'liquidacion', 'cobradorId': db.collection('usuarios').document(uid), 'cobrador': fullname(user), 'monto': float(delivered), 'reportado': float(expected), 'diferencia': float(delivered - expected), 'motivo': note, 'facturas': [d.id for d in selected], 'fecha': firestore.SERVER_TIMESTAMP, 'adminId': g.admin['id']})
            audit(tx, 'liquidar_cobrador', {'uid': uid, 'operacion': op, 'monto': float(delivered)})
        save(db.transaction())
        flash('Entrega registrada. Los recibos incluidos ya no están pendientes.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('panel', page='caja'))


@app.get('/exportar/<collection>')
@admin_required
def export(collection):
    if collection not in ('facturas', 'clientes', 'creditos', 'usuarios', 'movimientos', 'movimientosCaja'):
        abort(404)
    rows = read_collection(collection)
    start, end = period()
    if collection in ('facturas', 'movimientos', 'movimientosCaja'):
        rows = [r for r in rows if date_value(r.get('fecha')) and start <= date_value(r['fecha']).date() <= end]
    if request.args.get('cliente'):
        rows = [r for r in rows if ref_id(r.get('clienteId')) == request.args['cliente']]
    if request.args.get('cobrador'):
        rows = [r for r in rows if ref_id(r.get('cobradorId') or r.get('registradoPor')) == request.args['cobrador']]
    fields = sorted({key for row in rows for key in row}) or ['id']
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        clean = {}
        for key, value in row.items():
            value = value.path if hasattr(value, 'path') else value.isoformat() if isinstance(value, datetime) else str(value)
            if value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'" + value
            clean[key] = value
        writer.writerow(clean)
    return Response('\ufeff' + output.getvalue(), mimetype='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="cuotix-{collection}-{now():%Y%m%d}.csv"'})


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def user_error(error):
    return render_template('error.html', page='error', error=error.description, code=error.code), error.code


@app.errorhandler(Exception)
def service_error(error):
    if isinstance(error, HTTPException):
        return render_template('error.html', page='error', code=error.code, error=error.description), error.code
    app.logger.error('Error de servicio: %s', type(error).__name__)
    return render_template('error.html', page='error', code=503, error='No se pudo completar la operación. Verifica la conexión con Firebase y vuelve a intentarlo. Si estabas registrando una operación, revisa el historial antes de repetirla.'), 503


app.jinja_env.globals.update(number=number, ref_id=ref_id, receipt_amount=receipt_amount, operation_id=lambda: secrets.token_hex(16))

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', '1') == '1',
        port=int(os.environ.get('PORT', '8000')),
    )
