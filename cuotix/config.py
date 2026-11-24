import os
import secrets
from datetime import timedelta, timezone


LOCAL_TZ = timezone(timedelta(hours=-6))
COLLECTIONS = ('usuarios', 'clientes', 'creditos', 'cuotas', 'facturas', 'movimientos')
NAV = [
    ('monitor', '/', 'grid', 'Monitoreo & Cobros'),
    ('cobradores', '/cobradores', 'people', 'Gestión de Cobradores'),
    ('caja', '/caja', 'wallet', 'Inventario de dinero'),
    ('clientes', '/clientes', 'folder', 'Historial & Clientes'),
    ('reportes', '/reportes', 'chart', 'Reportes & Métricas'),
    ('apariencia', '/apariencia', 'palette', 'Paletas de color'),
]
TITLES = {
    'monitor': 'Centro de Monitoreo & Operaciones de Cobro',
    'cobradores': 'Control Maestro de Accesos & Roles',
    'caja': 'Inventario de dinero',
    'clientes': 'Historial Crediticio por Cliente',
    'reportes': 'Inteligencia Financiera, Reportes y Rendimiento de Cartera',
    'auditoria': 'Registro de auditoría',
    'apariencia': 'Paletas de color',
}
DENOMINATIONS = (1000, 500, 200, 100, 50, 20, 10)


class Config:
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.environ.get('COOKIE_SECURE') == '1'
    MAX_CONTENT_LENGTH = 64 * 1024
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=15)
