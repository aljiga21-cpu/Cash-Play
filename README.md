# ISLA SALVAJE - Telegram Mini App

## Archivos
- `bot.py`: bot de Telegram, API del juego, guardado JSON, PayPal y escena 3D.
- `requirements.txt`: dependencias Python.
- `Procfile`: comando de arranque para Render.

## Variables de entorno en Render
Obligatorias:
- `BOT_TOKEN`: token del bot de BotFather.
- `PUBLIC_URL`: URL pública completa del servicio, por ejemplo `https://mi-juego.onrender.com`.

Opcionales:
- `WEBHOOK_SECRET`: secreto aleatorio para el webhook.
- `PAYPAL_CLIENT_ID`: Client ID de PayPal.
- `PAYPAL_CLIENT_SECRET`: Client Secret de PayPal.
- `PAYPAL_MODE`: `sandbox` para pruebas; `live` para cobros reales.
- `MONETAG_ZONE_ID`: zona de Monetag.
- `MONETAG_POSTBACK_SECRET`: secreto privado del postback.
- `DATA_FILE`: ruta del JSON; por defecto `./data/players.json`.

## Despliegue
1. Sube estos archivos a un repositorio GitHub.
2. En Render crea un **Web Service** conectado al repositorio.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn bot:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120`
5. Añade las variables de entorno.
6. Abre `https://TU-SERVICIO.onrender.com/health`.
7. En Telegram ejecuta `/start` en el bot.

## Pruebas de PayPal
Empieza con `PAYPAL_MODE=sandbox` y credenciales Sandbox. Haz compras de prueba antes de activar Live.

## Monetag
El endpoint `/api/monetag/postback` es una plantilla segura con secreto e idempotencia.
Hay que adaptar los nombres de parámetros y la verificación al formato exacto de postback que proporcione tu panel de Monetag. No se entregan monedas por una simple llamada JavaScript del navegador.

## Persistencia
El juego guarda los datos en `./data/players.json`. En Render sin disco persistente, el contenido puede perderse al reiniciar, desplegar o reemplazar la instancia. No es una base de datos.
