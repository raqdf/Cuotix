from firebase_config import firebase_app, get_db


def init_app(app):
    app.extensions['firebase_admin'] = firebase_app


__all__ = ['firebase_app', 'get_db']
