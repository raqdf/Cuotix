from decimal import Decimal

from firebase_admin import firestore
from flask import Blueprint, flash, g, redirect, request, url_for

from ..extensions import get_db
from ..security import admin_required
from ..utils import amount, audit, confirmed, fullname, number, physical_cash_count, receipt_amount, ref_id, required, valid_id


bp = Blueprint('cash', __name__)


@bp.post('/caja/inventario')
@admin_required
def save_cash_inventory():
    try:
        operation = valid_id(required('operacion'))
        counts, coins, total = physical_cash_count()
        reason = request.form.get('motivo', '').strip()[:500]
        db = get_db()
        cash_ref = db.collection('cajaAdmin').document('central')
        log_ref = db.collection('movimientosCaja').document(operation)

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
                'tipo': 'inventario',
                'monto': float(total),
                'saldoAnterior': float(balance),
                'diferencia': float(difference),
                'adminId': g.admin['id'],
                'adminNombre': g.admin.get('nombre', g.admin['id']),
                'fecha': firestore.SERVER_TIMESTAMP,
                'detalle': {
                    'billetes': counts,
                    'monedas': float(coins),
                    'observacion': reason,
                    'estadoCaja': cash.get('estado', 'sin apertura'),
                },
            })
            audit(tx, 'caja_inventario', {'operacion': operation, 'monto': float(total)})

        save(db.transaction())
        flash('Inventario guardado. Puedes consultar el desglose en el historial de dinero inventariado.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='caja') + '#inventoryHistory')


@bp.post('/caja/operacion')
@admin_required
def cash_operation():
    try:
        action = required('accion')
        operation = valid_id(required('operacion'))
        db = get_db()
        cash_ref = db.collection('cajaAdmin').document('central')
        log_ref = db.collection('movimientosCaja').document(operation)

        @firestore.transactional
        def operate(tx):
            cash = cash_ref.get(transaction=tx).to_dict() or {}
            if log_ref.get(transaction=tx).exists:
                return
            current, vault = number(cash.get('saldoCaja')), number(cash.get('saldoBoveda'))
            if action == 'apertura':
                if cash.get('estado') == 'abierta':
                    raise ValueError('La caja ya está abierta.')
                value = amount(required('monto'))
                if cash and value != current:
                    raise ValueError('La apertura debe coincidir con el último saldo de caja.')
                extra = {
                    'estado': 'abierta',
                    'saldoCaja': float(value),
                    'saldoBoveda': float(vault),
                    'fechaApertura': firestore.SERVER_TIMESTAMP,
                }
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
                    extra = {
                        'estado': 'cerrada',
                        'saldoCaja': float(value),
                        'ultimoConteo': counts,
                        'diferencia': float(difference),
                        'monedas': float(coins),
                        'fechaCierre': firestore.SERVER_TIMESTAMP,
                        'observacion': reason,
                    }
                else:
                    raise ValueError('Operación de caja desconocida.')
            tx.set(cash_ref, dict(extra, fechaActualizacion=firestore.SERVER_TIMESTAMP), merge=True)
            tx.set(log_ref, dict(
                tipo=action,
                monto=float(value),
                saldoAnterior=float(current),
                adminId=g.admin['id'],
                fecha=firestore.SERVER_TIMESTAMP,
                detalle=extra,
            ))
            audit(tx, 'caja_' + action, {'operacion': operation, 'monto': float(value)})

        operate(db.transaction())
        flash('Operación de caja registrada.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='caja'))


@bp.post('/caja/liquidar')
@admin_required
def settle():
    try:
        uid = valid_id(required('cobrador'))
        delivered = amount(required('monto'))
        note = request.form.get('motivo', '').strip()[:500]
        operation = valid_id(required('operacion'))
        db = get_db()
        cash_ref = db.collection('cajaAdmin').document('central')
        log_ref = db.collection('movimientosCaja').document(operation)

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
            credits = {
                document.id: document.to_dict()
                for document in db.collection('creditos').stream(transaction=tx)
            }
            selected = []
            for document in invoices:
                row = document.to_dict()
                collector_id = ref_id(
                    row.get('cobradorId')
                    or credits.get(ref_id(row.get('creditoId')), {}).get('cobradorId')
                )
                if (
                    collector_id == uid
                    and confirmed(row)
                    and str(row.get('metodoPago', row.get('tipoPago', ''))).lower() == 'efectivo'
                    and not row.get('liquidacionAdminId')
                ):
                    selected.append(document)
            if not selected:
                raise ValueError('No hay recibos de efectivo pendientes para este cobrador.')
            if len(selected) > 440:
                raise ValueError('Se requiere una liquidación por lotes (más de 440 recibos).')
            expected = sum((receipt_amount(document.to_dict()) for document in selected), Decimal(0))
            if delivered != expected and not note:
                raise ValueError('Explica la diferencia entre lo reportado y lo entregado.')
            tx.update(cash_ref, {
                'saldoCaja': float(number(cash.get('saldoCaja')) + delivered),
                'fechaActualizacion': firestore.SERVER_TIMESTAMP,
            })
            for document in selected:
                tx.update(document.reference, {'liquidacionAdminId': operation})
            tx.set(log_ref, {
                'tipo': 'liquidacion',
                'cobradorId': db.collection('usuarios').document(uid),
                'cobrador': fullname(user),
                'monto': float(delivered),
                'reportado': float(expected),
                'diferencia': float(delivered - expected),
                'motivo': note,
                'facturas': [document.id for document in selected],
                'fecha': firestore.SERVER_TIMESTAMP,
                'adminId': g.admin['id'],
            })
            audit(tx, 'liquidar_cobrador', {'uid': uid, 'operacion': operation, 'monto': float(delivered)})

        save(db.transaction())
        flash('Entrega registrada. Los recibos incluidos ya no están pendientes.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='caja'))
