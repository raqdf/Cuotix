from firebase_admin import firestore
from flask import Blueprint, flash, redirect, url_for
from google.cloud.firestore_v1.base_query import FieldFilter

from ..extensions import get_db
from ..security import admin_required
from ..utils import active, audit, fullname, required, valid_id


bp = Blueprint('clients', __name__)


@bp.post('/clientes/<client_id>/cobrador')
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
                    documents.update({
                        document.reference.path: document
                        for document in query.stream(transaction=tx)
                    })
                return documents

            client_values = (client_ref, client_id, client_ref.path)
            credits = related_documents('creditos', 'clienteId', client_values)
            credit_values = [
                value
                for document in credits.values()
                for value in (document.reference, document.id, document.reference.path)
            ]
            quotas = related_documents('cuotas', 'clienteId', client_values)
            quotas.update(related_documents('cuotas', 'creditoId', credit_values))
            if 1 + len(credits) + len(quotas) + 1 > 450:
                raise ValueError('La cartera requiere una reasignación por lotes (más de 450 documentos).')

            timestamp = firestore.SERVER_TIMESTAMP
            tx.update(client_ref, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            for document in credits.values():
                tx.update(document.reference, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            for document in quotas.values():
                tx.update(document.reference, {'cobradorId': collector_ref, 'fechaActualizacion': timestamp})
            audit(tx, 'asignar_cliente_cobrador', {
                'clienteId': client_id,
                'cobradorId': collector_id,
                'creditos': len(credits),
                'cuotas': len(quotas),
            })
            return fullname(collector), len(credits), len(quotas)

        collector_name, credit_count, quota_count = assign(db.transaction())
        flash(
            f'Cliente asignado a {collector_name}. Se actualizaron {credit_count} créditos y '
            f'{quota_count} cuotas; los recibos históricos no cambiaron.',
            'success',
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='clientes', cliente=client_id))
