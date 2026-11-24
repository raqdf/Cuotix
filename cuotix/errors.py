from flask import current_app, render_template
from werkzeug.exceptions import HTTPException


def register_error_handlers(app):
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def user_error(error):
        return render_template('error.html', page='error', error=error.description, code=error.code), error.code

    @app.errorhandler(Exception)
    def service_error(error):
        if isinstance(error, HTTPException):
            return render_template('error.html', page='error', code=error.code, error=error.description), error.code
        current_app.logger.error('Error de servicio: %s', type(error).__name__)
        return render_template(
            'error.html',
            page='error',
            code=503,
            error='No se pudo completar la operación. Verifica la conexión con Firebase y vuelve a intentarlo. Si estabas registrando una operación, revisa el historial antes de repetirla.',
        ), 503
