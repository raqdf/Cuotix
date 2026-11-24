from .auth import bp as auth_bp
from .cash import bp as cash_bp
from .clients import bp as clients_bp
from .collectors import bp as collectors_bp
from .dashboard import bp as dashboard_bp


__all__ = [
    'auth_bp',
    'cash_bp',
    'clients_bp',
    'collectors_bp',
    'dashboard_bp',
]
