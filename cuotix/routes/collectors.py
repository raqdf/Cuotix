from firebase_admin import auth, firestore
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from google.cloud.firestore_v1.base_query import FieldFilter

from ..extensions import firebase_app, get_db
from ..security import admin_required
from ..services import build_context, load_data, resolve_collector_emails
from ..utils import active, amount, audit, fullname, required, valid_id


bp = Blueprint('collectors', __name__)


@bp.post('/cobradores/crear')
@admin_required
def create_collector():
    account = None
    try:
        fields = {key: required(key) for key in ('nombres', 'apellidos', 'cedula', 'numero', 'email')}
        password = required('password', 128)
        if len(password) < 8:
            raise ValueError('La contraseña temporal debe tener al menos 8 caracteres.')
        goal = amount(request.form.get('metaDiaria', '0'))
        account = auth.create_user(
            email=fields['email'],
            password=password,
            disabled=True,
            display_name=fields['nombres'] + ' ' + fields['apellidos'],
            app=firebase_app,
        )
        batch = get_db().batch()
        batch.set(get_db().collection('usuarios').document(account.uid), dict(
            fields,
            rol='cobrador',
            estado='pendiente',
            estadoSolicitado='activo',
            metaDiaria=float(goal),
            ruta=request.form.get('ruta', '').strip()[:100],
            fecha_Creacion=firestore.SERVER_TIMESTAMP,
        ))
        audit(batch, 'crear_cobrador', {'uid': account.uid})
        batch.commit()
        auth.update_user(account.uid, disabled=False, app=firebase_app)
        activation = get_db().batch()
        activation.update(get_db().collection('usuarios').document(account.uid), {
            'estado': 'activo',
            'estadoSolicitado': firestore.DELETE_FIELD,
        })
        audit(activation, 'activar_nuevo_cobrador', {'uid': account.uid})
        activation.commit()
        flash('Cobrador creado. Puede ingresar con su correo y contraseña temporal.', 'success')
    except Exception as exc:
        if account:
            flash(
                'El alta quedó pendiente. Si el cobrador aparece en la lista, usa Acceso → Reactivar. '
                'Si no aparece, revisa su cuenta en Firebase Authentication antes de repetir el alta.',
                'warning',
            )
        else:
            flash(
                str(exc) if isinstance(exc, ValueError)
                else 'No se pudo crear el cobrador. Verifica el correo y la conexión.',
                'danger',
            )
    return redirect(url_for('dashboard.panel', page='cobradores'))


@bp.post('/cobradores/<uid>/editar')
@admin_required
def edit_collector(uid):
    try:
        ref = get_db().collection('usuarios').document(valid_id(uid))
        fields = {key: required(key) for key in ('nombres', 'apellidos', 'cedula', 'numero')}
        fields.update(
            ruta=request.form.get('ruta', '').strip()[:100],
            metaDiaria=float(amount(request.form.get('metaDiaria', '0'))),
        )

        @firestore.transactional
        def save(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if str(profile.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Solo se puede editar un cobrador desde este formulario.')
            if profile.get('estadoSolicitado') == 'eliminar':
                raise ValueError('El borrado está pendiente. Completa o reintenta el borrado antes de editar.')
            tx.update(ref, dict(fields, fechaActualizacion=firestore.SERVER_TIMESTAMP))
            audit(tx, 'editar_cobrador', {'uid': uid, 'campos': list(fields)})

        save(get_db().transaction())
        flash('Datos del cobrador actualizados.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='cobradores'))


@bp.post('/cobradores/<uid>/estado')
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

        @firestore.transactional
        def transfer(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if str(profile.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Solo puedes cambiar el estado de un cobrador.')
            if profile.get('estadoSolicitado') == 'eliminar':
                raise ValueError('El borrado está pendiente. Reintenta el borrado antes de cambiar el acceso.')
            documents = []
            if state != 'activo':
                for collection in ('clientes', 'creditos', 'cuotas'):
                    documents.extend(
                        get_db().collection(collection)
                        .where(filter=FieldFilter('cobradorId', '==', ref))
                        .stream(transaction=tx)
                    )
                if documents and (not target or target_id == uid):
                    raise ValueError(
                        'Este cobrador tiene cartera asignada. Selecciona otro cobrador activo; '
                        'si no hay ninguno, créalo o reactívalo antes de cambiar el estado.'
                    )
                if target:
                    destination = target.get(transaction=tx).to_dict() or {}
                    if (str(destination.get('rol', '')).lower() != 'cobrador'
                            or not active(destination) or target_id == uid):
                        raise ValueError('El destino debe ser otro cobrador activo.')
                if len(documents) > 450:
                    raise ValueError('La cartera requiere una migración por lotes (más de 450 documentos).')
            for document in documents:
                tx.update(document.reference, {
                    'cobradorId': target,
                    'fechaActualizacion': firestore.SERVER_TIMESTAMP,
                })
            tx.update(ref, {
                'estado': 'pendiente',
                'estadoSolicitado': state,
                'motivoEstado': reason,
                'fechaActualizacion': firestore.SERVER_TIMESTAMP,
            })
            audit(tx, 'solicitar_estado_cobrador', {
                'uid': uid,
                'estado': state,
                'motivo': reason,
                'destino': target_id,
                'documentos': len(documents),
            })

        transfer(get_db().transaction())
        try:
            auth.update_user(uid, disabled=state != 'activo', app=firebase_app)
            auth.revoke_refresh_tokens(uid, app=firebase_app)
            batch = get_db().batch()
            batch.update(ref, {
                'estado': state,
                'estadoSolicitado': firestore.DELETE_FIELD,
            })
            audit(batch, 'confirmar_estado_cobrador', {'uid': uid, 'estado': state})
            batch.commit()
            flash('Estado actualizado y sesiones revocadas en Firebase Authentication.', 'success')
        except Exception:
            flash('Cartera procesada; sincronización de acceso pendiente. Repite el cambio de estado para completarla.', 'warning')
    except ValueError as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('dashboard.panel', page='cobradores'))


@bp.post('/cobradores/<uid>/eliminar')
@admin_required
def delete_collector(uid):
    account_removed = False
    try:
        uid = valid_id(uid)
        if uid == g.admin['id']:
            raise ValueError('No puedes eliminar la cuenta administradora con esta opción.')
        db = get_db()
        ref = db.collection('usuarios').document(uid)

        @firestore.transactional
        def prepare_deletion(tx):
            profile = ref.get(transaction=tx).to_dict() or {}
            if str(profile.get('rol', '')).lower() != 'cobrador':
                raise ValueError('Solo se puede eliminar un cobrador.')

            for collection in ('clientes', 'creditos', 'cuotas'):
                for owner in (ref, uid, ref.path):
                    query = db.collection(collection).where(
                        filter=FieldFilter('cobradorId', '==', owner)).limit(1)
                    if next(query.stream(transaction=tx), None):
                        raise ValueError(
                            'Este cobrador tiene cartera asignada. Reasigna sus clientes, créditos y cuotas antes de eliminarlo.'
                        )

            if profile.get('estadoSolicitado') == 'eliminar':
                if profile.get('estado') != 'eliminacion_pendiente':
                    raise ValueError('El estado del borrado es inconsistente. Revisa el perfil antes de reintentar.')
            else:
                tx.update(ref, {
                    'estado': 'eliminacion_pendiente',
                    'estadoSolicitado': 'eliminar',
                    'fechaActualizacion': firestore.SERVER_TIMESTAMP,
                })
                audit(tx, 'solicitar_eliminacion_cobrador', {'uid': uid})
            return profile

        profile = prepare_deletion(db.transaction())
        try:
            auth.update_user(uid, disabled=True, app=firebase_app)
            auth.revoke_refresh_tokens(uid, app=firebase_app)
            auth.delete_user(uid, app=firebase_app)
        except auth.UserNotFoundError:
            current_app.logger.info('La cuenta de autenticación del cobrador %s ya no existe', uid)
        account_removed = True

        batch = db.batch()
        batch.delete(ref)
        audit(batch, 'eliminar_cobrador', {'uid': uid, 'nombre': fullname(profile)})
        batch.commit()
        flash('Cuenta y perfil eliminados. Se conservaron los registros financieros y de auditoría.', 'success')
    except ValueError as exc:
        flash(str(exc), 'danger')
    except Exception as exc:
        current_app.logger.error('No se pudo completar el borrado del cobrador: %s', type(exc).__name__)
        if account_removed:
            flash(
                'La cuenta de acceso se eliminó, pero el perfil o la auditoría siguen pendientes. '
                'Reintenta el borrado para completar la limpieza.',
                'warning',
            )
        else:
            flash(
                'El borrado quedó pendiente. Reinténtalo desde la lista; no se eliminaron los registros financieros ni de auditoría.',
                'warning',
            )
    return redirect(url_for('dashboard.panel', page='cobradores'))


@bp.post('/cobradores/<uid>/password')
@admin_required
def collector_password(uid):
    try:
        valid_id(uid)
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        if not password.strip() or len(password) < 8 or len(password) > 128:
            raise ValueError('La contraseña debe tener entre 8 y 128 caracteres.')
        if password != password_confirm:
            raise ValueError('Las contraseñas no coinciden.')
        ref = get_db().collection('usuarios').document(uid)
        profile = ref.get().to_dict() or {}
        if str(profile.get('rol', '')).lower() != 'cobrador':
            raise ValueError('Solo puedes cambiar la contraseña de un cobrador.')
        if profile.get('estadoSolicitado') == 'eliminar':
            raise ValueError('No puedes cambiar la contraseña mientras el borrado está pendiente.')
        auth.update_user(uid, password=password, app=firebase_app)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('dashboard.panel', page='cobradores'))
    except Exception as exc:
        current_app.logger.error('No se pudo cambiar la contraseña del cobrador: %s', type(exc).__name__)
        flash('No se pudo actualizar la contraseña en Firebase. Verifica la cuenta e inténtalo nuevamente.', 'danger')
        return redirect(url_for('dashboard.panel', page='cobradores'))

    warning = ''
    try:
        auth.revoke_refresh_tokens(uid, app=firebase_app)
        batch = get_db().batch()
        audit(batch, 'actualizar_contrasena_cobrador', {'uid': uid})
        batch.commit()
    except Exception as exc:
        current_app.logger.error(
            'La contraseña cambió, pero no se completó la revocación o auditoría: %s',
            type(exc).__name__,
        )
        warning = (
            'La contraseña se cambió, pero no se pudieron confirmar la revocación de sesiones '
            'y el registro de auditoría. Verifica Firebase antes de reintentar.'
        )
    data = load_data()
    resolve_collector_emails(data)
    context = build_context('cobradores', data)
    context.update(
        credential_notice=warning or 'La nueva contraseña aparece una sola vez en la fila del cobrador. Cópiala antes de salir o recargar.',
        credential_warning=bool(warning),
        revealed_password_uid=uid,
        revealed_password=password,
    )
    return render_template('cobradores.html', **context)
