import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import g, request, session
from firebase_admin import firestore

from .config import DENOMINATIONS, LOCAL_TZ
from .extensions import get_db


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
    tx.set(get_db().collection('auditoriaAdmin').document(), dict(
        accion=action,
        detalle=details,
        adminId=g.admin['id'],
        fecha=firestore.SERVER_TIMESTAMP,
    ))


def confirmed(row):
    return str(row.get('estado', '')).lower() not in ('anulado', 'reversado', 'cancelado')


def receipt_amount(row):
    return number(row.get('monto', row.get('totalTransaccion', row.get('abonoAplicado', 0))))


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


def operation_id():
    return secrets.token_hex(16)
