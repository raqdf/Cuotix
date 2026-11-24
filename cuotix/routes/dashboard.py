import csv
import io
from datetime import datetime

from firebase_admin import firestore
from flask import Blueprint, Response, abort, g, render_template, request

from ..extensions import get_db
from ..security import admin_required
from ..services import build_context, load_data, period, read_collection, resolve_collector_emails
from ..utils import date_value, now, ref_id


bp = Blueprint('dashboard', __name__)


def page_response(page):
    templates = {
        'monitor': 'index.html',
        'cobradores': 'cobradores.html',
        'caja': 'caja.html',
        'clientes': 'clientes.html',
        'reportes': 'reportes.html',
        'apariencia': 'apariencia.html',
    }
    data = load_data()
    if page == 'cobradores':
        resolve_collector_emails(data)
    return render_template(templates[page], **build_context(page, data))


@bp.get('/')
@admin_required
def home():
    return page_response('monitor')


@bp.get('/cobradores', defaults={'page': 'cobradores'})
@bp.get('/caja', defaults={'page': 'caja'})
@bp.get('/clientes', defaults={'page': 'clientes'})
@bp.get('/reportes', defaults={'page': 'reportes'})
@bp.get('/apariencia', defaults={'page': 'apariencia'})
@admin_required
def panel(page):
    return page_response(page)


@bp.get('/auditoria')
@admin_required
def audit_page():
    records = [
        dict(document.to_dict(), id=document.id)
        for document in get_db().collection('auditoriaAdmin')
        .order_by('fecha', direction=firestore.Query.DESCENDING)
        .limit(100).stream()
    ]
    return render_template('auditoria.html', page='auditoria', records=records, admin=g.admin)


@bp.get('/exportar/<collection>')
@admin_required
def export(collection):
    if collection not in ('facturas', 'clientes', 'creditos', 'usuarios', 'movimientos', 'movimientosCaja'):
        abort(404)
    rows = read_collection(collection)
    start, end = period()
    if collection in ('facturas', 'movimientos', 'movimientosCaja'):
        rows = [
            row for row in rows
            if date_value(row.get('fecha')) and start <= date_value(row['fecha']).date() <= end
        ]
    if request.args.get('cliente'):
        rows = [row for row in rows if ref_id(row.get('clienteId')) == request.args['cliente']]
    if request.args.get('cobrador'):
        rows = [
            row for row in rows
            if ref_id(row.get('cobradorId') or row.get('registradoPor')) == request.args['cobrador']
        ]
    fields = sorted({key for row in rows for key in row}) or ['id']
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        clean = {}
        for key, value in row.items():
            value = (
                value.path if hasattr(value, 'path')
                else value.isoformat() if isinstance(value, datetime)
                else str(value)
            )
            if value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'" + value
            clean[key] = value
        writer.writerow(clean)
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="cuotix-{collection}-{now():%Y%m%d}.csv"'},
    )
