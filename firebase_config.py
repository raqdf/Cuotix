import json
import logging
import os
from pathlib import Path

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from dotenv import load_dotenv


project_dir = Path(__file__).resolve().parent
load_dotenv(project_dir / '.env')

service_account_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
credentials_path_value = os.environ.get('FIREBASE_CREDENTIALS_PATH')

if service_account_json:
    try:
        service_account_info = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise ValueError('FIREBASE_SERVICE_ACCOUNT_JSON debe contener JSON válido.') from exc
    cred = credentials.Certificate(service_account_info)
    firebase_app = firebase_admin.initialize_app(cred)
else:
    credentials_path = Path(credentials_path_value or project_dir / 'serviceAccountKey.json').expanduser()
    if not credentials_path.is_absolute():
        credentials_path = project_dir / credentials_path

    if credentials_path.is_file():
        cred = credentials.Certificate(str(credentials_path))
        firebase_app = firebase_admin.initialize_app(cred)
    else:
        firebase_app = None
        logging.getLogger(__name__).warning(
            'Firebase no esta configurado: define FIREBASE_SERVICE_ACCOUNT_JSON '
            'o FIREBASE_CREDENTIALS_PATH.'
        )


def get_db():
    if firebase_app is None:
        raise RuntimeError(
            'Firebase no esta configurado. Define FIREBASE_SERVICE_ACCOUNT_JSON '
            'o FIREBASE_CREDENTIALS_PATH.'
        )
    return firestore.client(app=firebase_app)
