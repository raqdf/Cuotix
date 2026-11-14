# Guía y Especificaciones del Proyecto: Cuotix

## Descripción General
Cuotix es una aplicación web desarrollada con Python y el microframework Flask, utilizando plantillas HTML y Bootstrap 5 para el diseño visual.

## Arquitectura y Estructura de Carpetas
El proyecto debe mantener estrictamente la siguiente estructura:
CUOTIX/
│
├── static/
│   ├── css/
│   └── js/
│
├── templates/
│   ├── index.html
│   └── contacto.html
│
├── venv/
└── app.py

## Reglas de Desarrollo para el Agente
1. **Backend:** Utilizar siempre Flask. Las rutas principales deben ir en `app.py` utilizando `render_template` para cargar las vistas desde la carpeta `templates/`.
2. **Frontend:** Usar clases de Bootstrap 5 para el diseño de las interfaces de usuario. Los CDN oficiales de Bootstrap deben estar incluidos en el `<head>` de todas las plantillas HTML.
3. **Entorno de ejecución:** El servidor debe configurarse para correr en modo depuración (debug=True) y utilizando el puerto `8000`:
   ```python
   if __name__ == '__main__':
       app.run(debug=True, port=8000)