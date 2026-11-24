from datetime import datetime, timedelta
from decimal import Decimal

from firebase_admin import auth
from flask import abort, current_app, g, request

from .config import COLLECTIONS, LOCAL_TZ
from .extensions import firebase_app, get_db
from .utils import active, confirmed, date_value, fullname, now, number, receipt_amount, ref_id


def read_collection(name):
    return [dict(doc.to_dict(), id=doc.id) for doc in get_db().collection(name).stream(timeout=20)]


def load_data():
    data = {name: read_collection(name) for name in COLLECTIONS}
    users = {u['id']: u for u in data['usuarios']}
    clients = {c['id']: c for c in data['clientes']}
    credits = {c['id']: c for c in data['creditos']}
    for user in data['usuarios']:
        user.update(nombre=fullname(user), activo=active(user))
        user['cartera'] = sum(
            (number(credit.get('saldoPendiente')) for credit in data['creditos']
             if ref_id(credit.get('cobradorId')) == user['id']),
            Decimal(0),
        )
        user['contratos'] = sum(
            1 for credit in data['creditos']
            if ref_id(credit.get('cobradorId')) == user['id']
            and number(credit.get('saldoPendiente')) > 0
        )
    for client in data['clientes']:
        client.update(
            nombre=fullname(client),
            cobrador=users.get(ref_id(client.get('cobradorId')), {}).get('nombre', 'Sin asignar'),
        )
    for collection in ('creditos', 'cuotas', 'facturas', 'movimientos'):
        for row in data[collection]:
            if collection == 'cuotas':
                row['numeroCuota'] = int(number(row.get('numeroCuota')))
            credit = credits.get(ref_id(row.get('creditoId')), {})
            client_id = ref_id(row.get('clienteId') or credit.get('clienteId'))
            collector_id = ref_id(row.get('cobradorId') or row.get('registradoPor') or credit.get('cobradorId'))
            row.update(
                cliente_id=client_id,
                cobrador_id=collector_id,
                cliente=clients.get(client_id, {}).get('nombre')
                or row.get('clienteNombre') or row.get('nombreCliente', 'Sin vincular'),
                cobrador=users.get(collector_id, {}).get('nombre')
                or row.get('cobradorNombre') or row.get('nombreCobrador', 'Sin vincular'),
            )
    data['cobradores'] = [user for user in data['usuarios'] if str(user.get('rol', '')).lower() == 'cobrador']
    return data


def resolve_collector_emails(data):
    for collector in data['cobradores']:
        if not str(collector.get('email') or '').strip():
            try:
                collector['email'] = auth.get_user(collector['id'], app=firebase_app).email or ''
            except auth.UserNotFoundError:
                current_app.logger.warning('No se encontró la cuenta de autenticación del cobrador %s', collector['id'])


def period():
    try:
        start = datetime.strptime(request.args.get('desde') or now().date().replace(day=1).isoformat(), '%Y-%m-%d').date()
        end = datetime.strptime(request.args.get('hasta') or now().date().isoformat(), '%Y-%m-%d').date()
        if start > end:
            raise ValueError()
        return start, end
    except ValueError:
        abort(400, 'El rango de fechas no es válido.')


def build_context(page, data):
    start, end = period()
    collector = request.args.get('cobrador', '')
    query = request.args.get('q', '').strip().casefold()
    receipts = [row for row in data['facturas'] if confirmed(row)]
    filtered = [
        row for row in receipts
        if date_value(row.get('fecha'))
        and start <= date_value(row['fecha']).date() <= end
        and (not collector or row['cobrador_id'] == collector)
    ]
    filtered.sort(key=lambda row: date_value(row['fecha']), reverse=True)
    today_receipts = [
        row for row in receipts
        if date_value(row.get('fecha'))
        and date_value(row['fecha']).date() == now().date()
        and (not collector or row['cobrador_id'] == collector)
    ]
    credits = [credit for credit in data['creditos'] if not collector or credit['cobrador_id'] == collector]
    overdue = [
        quota for quota in data['cuotas']
        if number(quota.get('saldoPendiente')) > 0
        and date_value(quota.get('fechaVencimiento'))
        and date_value(quota['fechaVencimiento']).date() < now().date()
        and (not collector or quota['cobrador_id'] == collector)
    ]
    portfolio = sum((number(credit.get('saldoPendiente')) for credit in credits), Decimal(0))
    overdue_total = sum((number(quota.get('saldoPendiente')) for quota in overdue), Decimal(0))
    stats = dict(
        collected=sum((receipt_amount(row) for row in filtered), Decimal(0)),
        today=sum((receipt_amount(row) for row in today_receipts), Decimal(0)),
        count=len(filtered),
        today_count=len(today_receipts),
        portfolio=portfolio,
        overdue=overdue_total,
        overdue_count=len(overdue),
        overdue_rate=float(overdue_total / portfolio * 100) if portfolio else 0,
        active_credits=sum(number(credit.get('saldoPendiente')) > 0 for credit in credits),
        placed=sum(
            (number(credit.get('monto')) for credit in credits
             if date_value(credit.get('fechaCreacion'))
             and start <= date_value(credit['fechaCreacion']).date() <= end),
            Decimal(0),
        ),
    )
    for user in data['cobradores']:
        user['cobrado'] = sum(
            (receipt_amount(row) for row in filtered if row['cobrador_id'] == user['id']),
            Decimal(0),
        )
        user['hoy'] = sum(
            (receipt_amount(row) for row in today_receipts if row['cobrador_id'] == user['id']),
            Decimal(0),
        )
        user['avance'] = min(100, float(user['hoy'] / number(user.get('metaDiaria')) * 100)) if number(user.get('metaDiaria')) else 0
    visible_receipts = [
        row for row in filtered
        if not query or query in ' '.join(
            str(row.get(key, '')) for key in ('cliente', 'cobrador', 'numeroFactura', 'creditoCodigo', 'id')
        ).casefold()
    ]
    visible_users = [
        user for user in data['cobradores']
        if not query or query in (user['nombre'] + ' ' + str(user.get('cedula', '')) + ' ' + user['id']).casefold()
    ]
    state = request.args.get('estado', '')
    if state == 'activo':
        visible_users = [user for user in visible_users if user['activo']]
    elif state == 'inactivo':
        visible_users = [
            user for user in visible_users
            if not user['activo'] and str(user.get('estado', '')).lower() != 'baneado'
        ]
    elif state == 'baneado':
        visible_users = [user for user in visible_users if str(user.get('estado', '')).lower() == 'baneado']
    elif state:
        visible_users = []
    client_list = [
        client for client in data['clientes']
        if not query or query in (
            client['nombre'] + ' ' + str(client.get('cedula', '')) + ' ' + client['id']
        ).casefold()
    ]
    selected = next(
        (client for client in client_list if client['id'] == request.args.get('cliente')),
        client_list[0] if client_list else None,
    )
    client_credits = [credit for credit in data['creditos'] if selected and credit['cliente_id'] == selected['id']]
    client_receipts = [row for row in receipts if selected and row['cliente_id'] == selected['id']]
    client_receipts.sort(
        key=lambda row: date_value(row.get('fecha')) or datetime.min.replace(tzinfo=LOCAL_TZ),
        reverse=True,
    )
    cash = (get_db().collection('cajaAdmin').document('central').get().to_dict() or {}) if page == 'caja' else {}
    cash_history = read_collection('movimientosCaja') if page == 'caja' else []
    cash_history.sort(
        key=lambda row: date_value(row.get('fecha')) or datetime.min.replace(tzinfo=LOCAL_TZ),
        reverse=True,
    )
    pending = [
        row for row in receipts
        if str(row.get('metodoPago', row.get('tipoPago', ''))).lower() == 'efectivo'
        and not row.get('liquidacionAdminId')
    ]
    for user in data['cobradores']:
        user['pendienteCaja'] = sum(
            (receipt_amount(row) for row in pending if row['cobrador_id'] == user['id']),
            Decimal(0),
        )
    trend = []
    for offset in range(6, -1, -1):
        day = now().date() - timedelta(days=offset)
        value = sum(
            (receipt_amount(row) for row in receipts
             if date_value(row.get('fecha'))
             and date_value(row['fecha']).date() == day
             and (not collector or row['cobrador_id'] == collector)),
            Decimal(0),
        )
        trend.append({'label': day.strftime('%d/%m'), 'value': float(value)})
    return dict(
        page=page,
        data=data,
        stats=stats,
        receipts=visible_receipts,
        users=visible_users,
        clients=client_list,
        selected=selected,
        client_credits=client_credits,
        client_receipts=client_receipts,
        cash=cash,
        cash_history=cash_history[:30],
        inventory_history=[row for row in cash_history if row.get('tipo') == 'inventario'],
        pending_cash=sum((receipt_amount(row) for row in pending), Decimal(0)),
        trend=trend,
        q=request.args.get('q', ''),
        start=start,
        end=end,
        collector=collector,
        active_collectors=[user for user in data['cobradores'] if user['activo']],
        updated=now(),
        admin=g.admin,
    )
