from flask import Flask

from .extensions import init_app as init_extensions
from .config import Config
from .errors import register_error_handlers
from .routes import auth_bp, cash_bp, clients_bp, collectors_bp, dashboard_bp
from .security import register_security_hooks
from .utils import datefmt, money, number, operation_id, receipt_amount, ref_id


def create_app():
    app = Flask(
        __name__,
        template_folder='../templates',
        static_folder='../static',
    )
    app.config.from_object(Config)
    init_extensions(app)
    app.jinja_env.filters['money'] = money
    app.jinja_env.filters['datefmt'] = datefmt
    app.jinja_env.globals.update(
        number=number,
        ref_id=ref_id,
        receipt_amount=receipt_amount,
        operation_id=operation_id,
    )

    register_security_hooks(app)
    register_error_handlers(app)
    for blueprint in (auth_bp, dashboard_bp, collectors_bp, clients_bp, cash_bp):
        app.register_blueprint(blueprint)
    return app
