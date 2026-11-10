"""
SUPERVIVENCIA 3D - Telegram Mini App
Backend de un solo archivo: Flask + Telegram Bot API + JSON + PayPal.

Variables de entorno obligatorias:
  BOT_TOKEN
  PUBLIC_URL                 URL pública de Render, por ejemplo https://mi-juego.onrender.com

Variables opcionales:
  WEBHOOK_SECRET             Secreto aleatorio para proteger el webhook de Telegram
  PAYPAL_CLIENT_ID
  PAYPAL_CLIENT_SECRET
  PAYPAL_MODE                sandbox (por defecto) o live
  PAYPAL_WEBHOOK_ID          Opcional; para verificación adicional de webhooks
  MONETAG_ZONE_ID            ID de zona Monetag (12235771)
  MONETAG_POSTBACK_SECRET    Secreto para el postback de recompensas Monetag
  DATA_FILE                  Por defecto ./data/players.json

IMPORTANTE:
- El JSON se guarda en el sistema de archivos local de Render. En un servicio sin disco
  persistente puede perderse al reiniciar, desplegar o mover la instancia.
- Las recompensas Monetag SOLO se abonan mediante el postback del servidor. No confíes
  en una señal JavaScript del navegador como prueba de que se vio un anuncio.
- PayPal se cobra en el servidor. Nunca pongas el Client Secret en JavaScript.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify, make_response, request

# ----------------------------- CONFIGURACIÓN -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox").strip().lower()
PAYPAL_API = (
    "https://api-m.paypal.com"
    if PAYPAL_MODE == "live"
    else "https://api-m.sandbox.paypal.com"
)
MONETAG_ZONE_ID = os.getenv("MONETAG_ZONE_ID", "").strip()
MONETAG_POSTBACK_SECRET = os.getenv("MONETAG_POSTBACK_SECRET", "").strip()
DATA_FILE = Path(os.getenv("DATA_FILE", "./data/players.json"))

GAME_TITLE = "ISLA SALVAJE"
START_COINS = 100
MAX_INITDATA_AGE_SECONDS = 24 * 60 * 60

PRODUCTS = {
    "starter_pack": {
        "title": "Pack de superviviente",
        "description": "500 monedas + 3 botiquines",
        "price": "1.99",
        "coins": 500,
        "medkits": 3,
    },
    "iron_axe": {
        "title": "Hacha reforzada",
        "description": "Herramienta mejorada para recolectar madera",
        "price": "2.49",
        "upgrade": "iron_axe",
    },
    "vip_7": {
        "title": "Superviviente VIP (7 días)",
        "description": "Bonificación diaria de monedas durante 7 días",
        "price": "3.99",
        "vip_days": 7,
    },
}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("supervivencia3d")
store_lock = threading.RLock()
webhook_setup_lock = threading.Lock()
webhook_ready = False


# ----------------------------- ALMACENAMIENTO JSON -----------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_store() -> dict:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        return {"players": {}, "pending_orders": {}, "processed_rewards": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("El archivo JSON no contiene un objeto")
        data.setdefault("players", {})
        data.setdefault("pending_orders", {})
        data.setdefault("processed_rewards", [])
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        # No sobrescribimos automáticamente un archivo dañado.
        log.exception("No se pudo leer %s: %s", DATA_FILE, exc)
        raise RuntimeError("El archivo de datos está dañado o no se puede leer") from exc


def save_store(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = DATA_FILE.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, DATA_FILE)


def new_player(user: dict) -> dict:
    return {
        "id": str(user["id"]),
        "username": user.get("username", ""),
        "first_name": user.get("first_name", "Superviviente"),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "level": 1,
        "xp": 0,
        "coins": START_COINS,
        "health": 100,
        "hunger": 100,
        "thirst": 100,
        "energy": 100,
        "wood": 0,
        "stone": 0,
        "food": 2,
        "water": 2,
        "medkits": 0,
        "inventory": [],
        "buildings": {"campfire": 0, "shelter": 0},
        "upgrades": [],
        "vip_until": 0,
        "last_daily": "",
        "last_action": 0,
    }


def get_or_create_player(data: dict, user: dict) -> dict:
    uid = str(user["id"])
    if uid not in data["players"]:
        data["players"][uid] = new_player(user)
    player = data["players"][uid]
    player["username"] = user.get("username", player.get("username", ""))
    player["first_name"] = user.get("first_name", player.get("first_name", "Superviviente"))
    player["updated_at"] = utc_now()
    return player


def public_player(player: dict) -> dict:
    # No se exponen datos de pago, tokens ni información interna.
    keys = (
        "id", "username", "first_name", "level", "xp", "coins", "health",
        "hunger", "thirst", "energy", "wood", "stone", "food", "water",
        "medkits", "inventory", "buildings", "upgrades", "vip_until",
    )
    return {key: player.get(key) for key in keys}


# ----------------------------- TELEGRAM -----------------------------

def telegram_api(method: str, payload: dict | None = None) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("Falta BOT_TOKEN")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API: {result}")
    return result


def set_telegram_webhook() -> None:
    global webhook_ready
    if webhook_ready or not BOT_TOKEN or not PUBLIC_URL:
        return
    with webhook_setup_lock:
        if webhook_ready:
            return
        payload = {"url": f"{PUBLIC_URL}/telegram/webhook"}
        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET
        try:
            telegram_api("setWebhook", payload)
            webhook_ready = True
            log.info("Webhook de Telegram configurado")
        except Exception:
            log.exception("No se pudo configurar el webhook de Telegram")


def send_message(chat_id: int | str, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_api("sendMessage", payload)


def mini_app_keyboard() -> dict:
    if not PUBLIC_URL:
        return {"keyboard": [[{"text": "El juego aún no está configurado"}]], "resize_keyboard": True}
    # Inline Web App button: Telegram attaches signed WebApp initData to this launch.
    return {
        "inline_keyboard": [[{
            "text": "🌴 JUGAR A ISLA SALVAJE",
            "web_app": {"url": f"{PUBLIC_URL}/"}
        }]]
    }


def handle_telegram_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    text = (message.get("text") or "").strip()
    if not chat_id or not user.get("id"):
        return

    if text.startswith("/start"):
        with store_lock:
            data = load_store()
            get_or_create_player(data, user)
            save_store(data)
        send_message(
            chat_id,
            "🏝️ ¡Bienvenido a ISLA SALVAJE!\n\n"
            "Sobrevive, recoge recursos, construye tu refugio y mejora tu equipo.\n"
            "Pulsa el botón para entrar en la isla.",
            mini_app_keyboard(),
        )
    elif text.startswith("/help"):
        send_message(
            chat_id,
            "Comandos disponibles:\n/start - Abrir el juego\n/help - Ayuda\n"
            "Tu progreso se guarda en un archivo JSON local."
        )
    else:
        send_message(chat_id, "Pulsa «🌴 JUGAR A ISLA SALVAJE» para continuar.", mini_app_keyboard())


# ----------------------------- AUTENTICACIÓN MINI APP -----------------------------

def validate_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise ValueError("El bot no está configurado")
    if not init_data:
        raise ValueError("Falta la autenticación de Telegram")

    from urllib.parse import parse_qsl
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Firma de Telegram ausente")

    auth_date = int(parsed.get("auth_date", "0"))
    if auth_date <= 0 or time.time() - auth_date > MAX_INITDATA_AGE_SECONDS:
        raise ValueError("La sesión de Telegram ha caducado")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Firma de Telegram no válida")

    user_json = parsed.get("user")
    if not user_json:
        raise ValueError("No se ha recibido el usuario de Telegram")
    user = json.loads(user_json)
    if not user.get("id"):
        raise ValueError("Usuario de Telegram no válido")
    return user


def authenticated_user() -> dict:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return validate_telegram_init_data(init_data)


# ----------------------------- PAYPAL -----------------------------

def paypal_access_token() -> str:
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PayPal no está configurado en Render")
    response = requests.post(
        f"{PAYPAL_API}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def paypal_request(method: str, path: str, payload: dict | None = None) -> dict:
    token = paypal_access_token()
    response = requests.request(
        method,
        f"{PAYPAL_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(uuid.uuid4()),
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


@app.post("/api/paypal/create-order")
def paypal_create_order():
    try:
        user = authenticated_user()
        body = request.get_json(silent=True) or {}
        product_id = str(body.get("product_id", ""))
        product = PRODUCTS.get(product_id)
        if not product:
            return jsonify(error="Producto no válido"), 400

        order = paypal_request("POST", "/v2/checkout/orders", {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": product_id,
                "description": product["description"],
                "custom_id": str(user["id"]),
                "amount": {"currency_code": "EUR", "value": product["price"]},
            }],
            "application_context": {
                "brand_name": GAME_TITLE,
                "user_action": "PAY_NOW",
                "return_url": f"{PUBLIC_URL}/?paypal=success",
                "cancel_url": f"{PUBLIC_URL}/?paypal=cancel",
            },
        })

        with store_lock:
            data = load_store()
            data["pending_orders"][order["id"]] = {
                "user_id": str(user["id"]),
                "product_id": product_id,
                "status": "CREATED",
                "created_at": utc_now(),
            }
            save_store(data)

        approval = next((link["href"] for link in order.get("links", []) if link.get("rel") == "approve"), None)
        if not approval:
            return jsonify(error="PayPal no devolvió el enlace de aprobación"), 502
        return jsonify(order_id=order["id"], approval_url=approval)
    except requests.HTTPError as exc:
        log.exception("Error creando pedido PayPal")
        return jsonify(error="PayPal no ha podido crear el pedido", detail=str(exc)), 502
    except Exception as exc:
        log.exception("Error en PayPal")
        return jsonify(error=str(exc)), 400


@app.post("/api/paypal/capture-order")
def paypal_capture_order():
    try:
        user = authenticated_user()
        body = request.get_json(silent=True) or {}
        order_id = str(body.get("order_id", ""))
        if not order_id:
            return jsonify(error="Falta order_id"), 400

        with store_lock:
            data = load_store()
            pending = data["pending_orders"].get(order_id)
            if not pending or pending["user_id"] != str(user["id"]):
                return jsonify(error="Pedido no encontrado para este jugador"), 404
            if pending["status"] == "COMPLETED":
                player = data["players"].get(str(user["id"]))
                return jsonify(ok=True, already_captured=True, player=public_player(player))

        captured = paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", {})
        if captured.get("status") != "COMPLETED":
            return jsonify(error="PayPal aún no confirma el pago", status=captured.get("status")), 402

        # Verificamos importe, moneda y referencia devueltos por PayPal antes de entregar.
        purchase_units = captured.get("purchase_units", [])
        captures = [
            cap for unit in purchase_units
            for cap in unit.get("payments", {}).get("captures", [])
        ]
        if not captures or any(cap.get("status") != "COMPLETED" for cap in captures):
            return jsonify(error="No se ha confirmado la captura del pago"), 402

        with store_lock:
            data = load_store()
            pending = data["pending_orders"].get(order_id)
            if not pending or pending["user_id"] != str(user["id"]):
                return jsonify(error="Pedido no válido"), 404
            if pending["status"] != "COMPLETED":
                player = data["players"].get(str(user["id"]))
                if not player:
                    return jsonify(error="Jugador no encontrado"), 404
                product = PRODUCTS[pending["product_id"]]
                player["coins"] += int(product.get("coins", 0))
                player["medkits"] += int(product.get("medkits", 0))
                if product.get("upgrade") and product["upgrade"] not in player["upgrades"]:
                    player["upgrades"].append(product["upgrade"])
                if product.get("vip_days"):
                    player["vip_until"] = max(int(player.get("vip_until", 0)), int(time.time())) + product["vip_days"] * 86400
                player["updated_at"] = utc_now()
                pending["status"] = "COMPLETED"
                pending["completed_at"] = utc_now()
                save_store(data)
            return jsonify(ok=True, player=public_player(data["players"][str(user["id"])]))
    except requests.HTTPError as exc:
        log.exception("Error capturando pedido PayPal")
        return jsonify(error="PayPal no ha podido confirmar el cobro", detail=str(exc)), 502
    except Exception as exc:
        log.exception("Error capturando PayPal")
        return jsonify(error=str(exc)), 400


# ----------------------------- API DEL JUEGO -----------------------------

@app.get("/api/me")
def api_me():
    try:
        user = authenticated_user()
        with store_lock:
            data = load_store()
            player = get_or_create_player(data, user)
            save_store(data)
            return jsonify(player=public_player(player), products=PRODUCTS)
    except Exception as exc:
        return jsonify(error=str(exc)), 401


def apply_xp(player: dict, amount: int) -> None:
    player["xp"] += amount
    while player["xp"] >= player["level"] * 100:
        player["xp"] -= player["level"] * 100
        player["level"] += 1
        player["coins"] += 25


@app.post("/api/action")
def api_action():
    try:
        user = authenticated_user()
        body = request.get_json(silent=True) or {}
        action = str(body.get("action", ""))
        now = time.time()

        with store_lock:
            data = load_store()
            player = get_or_create_player(data, user)

            # Límite sencillo contra spam de acciones.
            if now - float(player.get("last_action", 0)) < 0.65:
                return jsonify(error="Espera un momento antes de repetir la acción"), 429
            player["last_action"] = now

            if action == "gather_wood":
                if player["energy"] < 5:
                    return jsonify(error="No tienes energía suficiente"), 400
                player["wood"] += 2 if "iron_axe" in player["upgrades"] else 1
                player["energy"] -= 5
                apply_xp(player, 8)
                message = "Has recogido madera."
            elif action == "gather_stone":
                if player["energy"] < 7:
                    return jsonify(error="No tienes energía suficiente"), 400
                player["stone"] += 1
                player["energy"] -= 7
                apply_xp(player, 10)
                message = "Has recogido piedra."
            elif action == "eat":
                if player["food"] < 1:
                    return jsonify(error="No tienes comida"), 400
                player["food"] -= 1
                player["hunger"] = min(100, player["hunger"] + 35)
                player["health"] = min(100, player["health"] + 3)
                message = "Has comido."
            elif action == "drink":
                if player["water"] < 1:
                    return jsonify(error="No tienes agua"), 400
                player["water"] -= 1
                player["thirst"] = min(100, player["thirst"] + 40)
                message = "Has bebido agua."
            elif action == "build_campfire":
                if player["wood"] < 5 or player["stone"] < 2:
                    return jsonify(error="Necesitas 5 madera y 2 piedras"), 400
                player["wood"] -= 5
                player["stone"] -= 2
                player["buildings"]["campfire"] += 1
                apply_xp(player, 25)
                message = "Has construido una hoguera."
            elif action == "build_shelter":
                if player["wood"] < 12 or player["stone"] < 5:
                    return jsonify(error="Necesitas 12 madera y 5 piedras"), 400
                player["wood"] -= 12
                player["stone"] -= 5
                player["buildings"]["shelter"] += 1
                apply_xp(player, 50)
                message = "Has construido un refugio."
            elif action == "daily":
                today = datetime.now(timezone.utc).date().isoformat()
                if player.get("last_daily") == today:
                    return jsonify(error="Ya has recogido la recompensa diaria"), 400
                player["last_daily"] = today
                player["coins"] += 50
                player["food"] += 1
                player["water"] += 1
                message = "Recompensa diaria: 50 monedas, comida y agua."
            elif action == "rest":
                player["energy"] = min(100, player["energy"] + 25)
                message = "Has descansado y recuperado energía."
            else:
                return jsonify(error="Acción desconocida"), 400

            player["updated_at"] = utc_now()
            save_store(data)
            return jsonify(ok=True, message=message, player=public_player(player))
    except Exception as exc:
        log.exception("Error en acción del juego")
        return jsonify(error=str(exc)), 400


@app.post("/api/monetag/postback")
def monetag_postback():
    """
    Endpoint genérico para el postback servidor-a-servidor de Monetag.
    Debes adaptar los nombres de parámetros al formato que indique tu panel Monetag.
    No acredites recompensas desde JavaScript del cliente.
    """
    if not MONETAG_POSTBACK_SECRET:
        return jsonify(error="Postback no configurado"), 503
    secret = request.args.get("secret", "")
    if not hmac.compare_digest(secret, MONETAG_POSTBACK_SECRET):
        return jsonify(error="No autorizado"), 403

    user_id = str(request.args.get("user_id", ""))
    transaction_id = str(request.args.get("tx_id", ""))
    if not user_id or not transaction_id:
        return jsonify(error="Faltan user_id o tx_id"), 400

    with store_lock:
        data = load_store()
        if transaction_id in data["processed_rewards"]:
            return jsonify(ok=True, duplicate=True)
        player = data["players"].get(user_id)
        if not player:
            return jsonify(error="Jugador no encontrado"), 404
        player["coins"] += 30
        player["energy"] = min(100, player["energy"] + 15)
        player["updated_at"] = utc_now()
        data["processed_rewards"].append(transaction_id)
        data["processed_rewards"] = data["processed_rewards"][-10000:]
        save_store(data)
    return jsonify(ok=True)


# ----------------------------- INTERFAZ 3D -----------------------------

GAME_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#101b19">
<title>ISLA SALVAJE - Supervivencia 3D</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{color-scheme:dark;--panel:rgba(12,24,22,.88);--line:rgba(255,255,255,.13);--green:#91d6a2}
*{box-sizing:border-box}body{margin:0;background:#101b19;color:#fff;font-family:system-ui,Arial;overflow:hidden}
#scene{position:fixed;inset:0;z-index:0}canvas{display:block}
.hud{position:fixed;z-index:2;left:10px;right:10px;top:10px;display:flex;gap:8px;align-items:flex-start;justify-content:space-between;pointer-events:none}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:9px 11px;backdrop-filter:blur(10px);box-shadow:0 6px 25px #0005}
.stats{display:grid;grid-template-columns:repeat(2,minmax(72px,1fr));gap:5px;font-size:12px;min-width:155px}
.stat{color:#d8e8df}.stat b{color:#fff;float:right}
.brand{font-weight:900;letter-spacing:1px;font-size:13px}.sub{font-size:10px;color:#b8c8bf}
.bottom{position:fixed;z-index:3;left:10px;right:10px;bottom:10px;display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
button{border:1px solid var(--line);background:rgba(19,38,32,.94);color:#fff;border-radius:12px;padding:11px 7px;font-weight:700;font-size:12px;min-height:43px}
button:active{transform:scale(.97);background:#315b42}button.primary{background:#267747;border-color:#4b9b69}
#toast{position:fixed;z-index:8;left:50%;top:75px;transform:translateX(-50%);background:#172c24;border:1px solid #ffffff30;border-radius:12px;padding:10px 14px;display:none;max-width:90%;font-size:13px;text-align:center}
#shop{position:fixed;z-index:6;inset:0;background:#0009;display:none;align-items:flex-end}
.sheet{width:100%;max-height:78vh;overflow:auto;background:#111f1b;border:1px solid #ffffff22;border-radius:20px 20px 0 0;padding:16px}
.item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid #ffffff18}
.item small{display:block;color:#b9c9c0;margin-top:3px}.item button{min-width:95px}
#hint{position:fixed;z-index:2;left:50%;bottom:178px;transform:translateX(-50%);background:#07120f99;padding:7px 12px;border-radius:20px;font-size:11px;color:#e5eee8;white-space:nowrap}
#joystick{position:fixed;z-index:4;left:20px;bottom:190px;width:116px;height:116px;border-radius:50%;border:2px solid #ffffff66;background:#07120f66;touch-action:none;display:grid;place-items:center;backdrop-filter:blur(4px)}
#stick{width:48px;height:48px;border-radius:50%;background:#d8eee0bb;border:2px solid #fff;box-shadow:0 3px 14px #0008;transform:translate(0,0)}
@media(min-width:700px){#joystick{left:calc(50% - 390px);bottom:125px}#hint{bottom:145px}}
@media(min-width:700px){.bottom{left:50%;right:auto;transform:translateX(-50%);width:min(560px,95vw)}.hud{max-width:900px;left:50%;right:auto;transform:translateX(-50%);width:calc(100% - 24px)}}
</style>
</head>
<body>
<div id="scene"></div>
<div class="hud">
 <div class="panel"><div class="brand">🌴 ISLA SALVAJE</div><div class="sub" id="level">Superviviente · Nivel 1</div><div class="sub" id="xp">0 / 100 XP</div></div>
 <div class="panel stats">
  <div class="stat">❤️ Vida <b id="health">100</b></div><div class="stat">🍖 Hambre <b id="hunger">100</b></div>
  <div class="stat">💧 Sed <b id="thirst">100</b></div><div class="stat">⚡ Energía <b id="energy">100</b></div>
  <div class="stat">🪙 Monedas <b id="coins">100</b></div><div class="stat">🪵 Madera <b id="wood">0</b></div>
  <div class="stat">🪨 Piedra <b id="stone">0</b></div><div class="stat">🍎 Comida <b id="food">2</b></div>
 </div>
</div>
<div id="hint">Joystick para moverte · Arrastra la derecha para mirar · Shift para correr</div>
<div id="joystick"><div id="stick"></div></div>
<div id="toast"></div>
<div class="bottom">
 <button onclick="act('gather_wood')">🪵 Recoger madera</button>
 <button onclick="act('gather_stone')">🪨 Recoger piedra</button>
 <button onclick="act('rest')">⚡ Descansar</button>
 <button onclick="act('eat')">🍎 Comer</button>
 <button onclick="act('drink')">💧 Beber</button>
 <button onclick="act('daily')">🎁 Premio diario</button>
 <button onclick="act('build_campfire')">🔥 Hoguera</button>
 <button onclick="act('build_shelter')">🏕️ Refugio</button>
 <button onclick="showRewardedAd()">🎬 Anuncio + recompensa</button>
 <button onclick="showPopupAd()">📺 Anuncio extra</button>
 <button onclick="openShop()">🛒 Tienda</button>
</div>
<div id="shop"><div class="sheet">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3>🛒 Tienda de supervivencia</h3><button onclick="closeShop()">Cerrar ✕</button></div>
 <div class="item"><div><b>Pack de superviviente</b><small>500 monedas + 3 botiquines · 1,99 €</small></div><button onclick="buy('starter_pack')">Comprar</button></div>
 <div class="item"><div><b>Hacha reforzada</b><small>Recoge el doble de madera · 2,49 €</small></div><button onclick="buy('iron_axe')">Comprar</button></div>
 <div class="item"><div><b>VIP 7 días</b><small>Ventajas VIP · 3,99 €</small></div><button onclick="buy('vip_7')">Comprar</button></div>
 <p class="sub">Los pagos se procesan en PayPal. Las mejoras se entregan después de confirmar el cobro.</p>
 </div></div>

<script type="module">
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
let player = null;
function telegramInitData(){
 // Read it at request time; do not cache an empty value during WebView startup.
 return window.Telegram?.WebApp?.initData || '';
}
function requestHeaders(){
 return {'Content-Type':'application/json','X-Telegram-Init-Data':telegramInitData()};
}
const toastEl = document.getElementById('toast');
function toast(msg){toastEl.textContent=msg;toastEl.style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>toastEl.style.display='none',2600)}
function renderPlayer(p){
 player=p;
 for(const k of ['health','hunger','thirst','energy','coins','wood','stone','food']) document.getElementById(k).textContent=p[k] ?? 0;
 document.getElementById('level').textContent=`${p.first_name || 'Superviviente'} · Nivel ${p.level}`;
 document.getElementById('xp').textContent=`${p.xp} / ${p.level*100} XP`;
}
async function api(url,body){
 const initData=telegramInitData();
 if(!initData) throw new Error('Telegram no ha entregado initData. Cierra esta ventana y vuelve a abrir el juego desde el botón del mensaje del bot.');
 const r=await fetch(url,{method:body?'POST':'GET',headers:requestHeaders(),...(body?{body:JSON.stringify(body)}:{})});
 const j=await r.json(); if(!r.ok) throw new Error(j.error||'Error de conexión'); return j;
}
async function load(){
 // Give Telegram WebView a short moment to initialize its signed launch data.
 for(let i=0;i<20 && !telegramInitData();i++) await new Promise(resolve=>setTimeout(resolve,150));
 try{const j=await api('/api/me');renderPlayer(j.player)}catch(e){toast(e.message)}
}
window.act=async(action)=>{try{const j=await api('/api/action',{action});renderPlayer(j.player);toast(j.message)}catch(e){toast(e.message)}};
window.openShop=()=>document.getElementById('shop').style.display='flex';
window.closeShop=()=>document.getElementById('shop').style.display='none';
window.buy=async(product_id)=>{
 try{
  const j=await api('/api/paypal/create-order',{product_id});
  tg?.openLink ? tg.openLink(j.approval_url) : window.open(j.approval_url,'_blank');
  toast('Completa el pago en PayPal. Al volver, pulsa actualizar.');
  sessionStorage.setItem('paypal_order',j.order_id);
 }catch(e){toast(e.message)}
};
// Monetag zone 12235771. La promesa indica que terminó el formato; la recompensa
// económica debe validarse además con el postback S2S configurado en el panel.
window.showRewardedAd=async()=>{
 try{
  if(typeof window.show_12235771!=='function') throw new Error('El SDK de Monetag no está cargado. Revisa la integración del script en Monetag.');
  await window.show_12235771();
  toast('Anuncio finalizado. La recompensa se confirmará mediante Monetag.');
 }catch(e){toast('No se pudo completar el anuncio: '+(e?.message||'error'))}
};
window.showPopupAd=async()=>{
 try{
  if(typeof window.show_12235771!=='function') throw new Error('El SDK de Monetag no está cargado. Revisa la integración del script en Monetag.');
  await window.show_12235771('pop');
  toast('Anuncio finalizado.');
 }catch(e){toast('No se pudo mostrar el anuncio: '+(e?.message||'error'))}
};
async function captureIfReturned(){
 const params=new URLSearchParams(location.search);
 if(params.get('paypal')==='success'){
  const orderId=params.get('token')||sessionStorage.getItem('paypal_order');
  if(orderId){try{const j=await api('/api/paypal/capture-order',{order_id:orderId});renderPlayer(j.player);toast('¡Compra confirmada! Mejora entregada.');sessionStorage.removeItem('paypal_order')}catch(e){toast('No se pudo confirmar el pago: '+e.message)}}
  history.replaceState({},'',location.pathname);
 }else if(params.get('paypal')==='cancel'){toast('Pago cancelado');history.replaceState({},'',location.pathname)}
}

// Escena 3D procedural. Es una primera versión jugable; los modelos detallados se añadirán después.
const host=document.getElementById('scene');
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x9dc4c4); scene.fog=new THREE.Fog(0x9dc4c4,32,115);
const camera=new THREE.PerspectiveCamera(65,innerWidth/innerHeight,.1,300);
camera.position.set(0,8,16);
const renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:'high-performance'});
renderer.setPixelRatio(Math.min(devicePixelRatio,2)); renderer.setSize(innerWidth,innerHeight); renderer.shadowMap.enabled=true; renderer.shadowMap.type=THREE.PCFSoftShadowMap;
renderer.outputColorSpace=THREE.SRGBColorSpace; renderer.toneMapping=THREE.ACESFilmicToneMapping; renderer.toneMappingExposure=1.12;
host.appendChild(renderer.domElement);
scene.add(new THREE.HemisphereLight(0xd9f3ff,0x44513a,2.1));
const sun=new THREE.DirectionalLight(0xffe4b5,3.0); sun.position.set(-20,35,15); sun.castShadow=true; sun.shadow.mapSize.set(2048,2048); sun.shadow.camera.left=-55; sun.shadow.camera.right=55; sun.shadow.camera.top=55; sun.shadow.camera.bottom=-55; scene.add(sun);
const ground=new THREE.Mesh(new THREE.PlaneGeometry(220,220),new THREE.MeshStandardMaterial({color:0x496c43,roughness:1}));ground.rotation.x=-Math.PI/2;ground.receiveShadow=true;scene.add(ground);
const sand=new THREE.Mesh(new THREE.CircleGeometry(22,64),new THREE.MeshStandardMaterial({color:0xc5b27d,roughness:1}));sand.rotation.x=-Math.PI/2;sand.position.y=.025;sand.scale.set(1.25,.85,1);scene.add(sand);
const water=new THREE.Mesh(new THREE.PlaneGeometry(220,220),new THREE.MeshStandardMaterial({color:0x287f91,roughness:.35,metalness:.1}));water.rotation.x=-Math.PI/2;water.position.y=-.12;scene.add(water);
const trunkMat=new THREE.MeshStandardMaterial({color:0x65452d}),leafMat=new THREE.MeshStandardMaterial({color:0x285b38,roughness:1});
function tree(x,z,s=1){
 const g=new THREE.Group();g.position.set(x,0,z);g.scale.setScalar(s);
 const trunk=new THREE.Mesh(new THREE.CylinderGeometry(.18,.28,2.4,7),trunkMat);trunk.position.y=1.2;trunk.castShadow=true;g.add(trunk);
 for(let i=0;i<5;i++){const crown=new THREE.Mesh(new THREE.ConeGeometry(1.25-i*.12,2.1,7),leafMat);crown.position.y=2.5+i*.34;crown.rotation.y=i*.8;crown.castShadow=true;g.add(crown)}
 scene.add(g);
}
for(let i=0;i<65;i++){const a=Math.random()*Math.PI*2,r=13+Math.random()*42;tree(Math.cos(a)*r,Math.sin(a)*r,.65+Math.random()*.8)}
const rockMat=new THREE.MeshStandardMaterial({color:0x777b76,roughness:1});
for(let i=0;i<28;i++){const a=Math.random()*Math.PI*2,r=8+Math.random()*36;const rock=new THREE.Mesh(new THREE.DodecahedronGeometry(.45+Math.random()*.8,0),rockMat);rock.position.set(Math.cos(a)*r,.35,Math.sin(a)*r);rock.scale.y=.65;rock.castShadow=true;scene.add(rock)}
// Superviviente 3D mejorado: torso, ropa, mochila, brazos, piernas, botas y rostro.
const playerMesh=new THREE.Group();
const skinMat=new THREE.MeshStandardMaterial({color:0xb97850,roughness:.78});
const shirtMat=new THREE.MeshStandardMaterial({color:0x405b43,roughness:.92});
const pantsMat=new THREE.MeshStandardMaterial({color:0x303b38,roughness:.95});
const leatherMat=new THREE.MeshStandardMaterial({color:0x493326,roughness:.9});
const bootMat=new THREE.MeshStandardMaterial({color:0x211e1a,roughness:.95});
function part(parent,geo,mat,pos,scale){const m=new THREE.Mesh(geo,mat);m.position.set(...pos);if(scale)m.scale.set(...scale);m.castShadow=true;m.receiveShadow=true;parent.add(m);return m;}
const torso=part(playerMesh,new THREE.CapsuleGeometry(.36,.68,6,12),shirtMat,[0,1.35,0],[1,1,0.78]);
part(playerMesh,new THREE.SphereGeometry(.245,24,18),skinMat,[0,2.12,0],[.92,1.12,.9]);
// pelo y rasgos faciales sencillos
const hairMat=new THREE.MeshStandardMaterial({color:0x241b15,roughness:1});
part(playerMesh,new THREE.SphereGeometry(.25,20,12,0,Math.PI*2,0,Math.PI*.48),hairMat,[0,2.22,-.015],[1.02,.75,1]);
const eyeMat=new THREE.MeshStandardMaterial({color:0x171411,roughness:.4});
part(playerMesh,new THREE.SphereGeometry(.025,10,8),eyeMat,[-.085,2.14,.215]);part(playerMesh,new THREE.SphereGeometry(.025,10,8),eyeMat,[.085,2.14,.215]);
// mochila
part(playerMesh,new THREE.BoxGeometry(.48,.58,.22),leatherMat,[0,1.38,-.31]);
part(playerMesh,new THREE.BoxGeometry(.32,.12,.24),new THREE.MeshStandardMaterial({color:0x8b6a3e}),[0,1.55,-.45]);
const arms=[],legs=[];
for(const side of [-1,1]){
 const armPivot=new THREE.Group();armPivot.position.set(side*.43,1.68,0);playerMesh.add(armPivot);part(armPivot,new THREE.CapsuleGeometry(.105,.48,5,10),shirtMat,[0,-.28,0]);part(armPivot,new THREE.SphereGeometry(.11,12,10),skinMat,[0,-.59,.015]);arms.push(armPivot);
 const legPivot=new THREE.Group();legPivot.position.set(side*.19,.88,0);playerMesh.add(legPivot);part(legPivot,new THREE.CapsuleGeometry(.14,.48,5,10),pantsMat,[0,-.25,0]);part(legPivot,new THREE.BoxGeometry(.25,.15,.4),bootMat,[0,-.56,.09]);legs.push(legPivot);
}
scene.add(playerMesh);
const camp=new THREE.Group();const logMat=new THREE.MeshStandardMaterial({color:0x5b3b25});
for(let i=0;i<3;i++){const log=new THREE.Mesh(new THREE.CylinderGeometry(.12,.12,1.4,8),logMat);log.rotation.z=Math.PI/2;log.rotation.y=i*Math.PI/3;log.position.set(3,.15,2);log.castShadow=true;camp.add(log)}
scene.add(camp);
let cameraAngle=0,dragging=false,lastX=0,lastY=0;
const keys={};let moveX=0,moveY=0,sprinting=false;
const joy=document.getElementById('joystick'),stick=document.getElementById('stick');
let joyPointer=null;
function setJoy(e){const r=joy.getBoundingClientRect(),cx=r.left+r.width/2,cy=r.top+r.height/2;let dx=e.clientX-cx,dy=e.clientY-cy;const len=Math.hypot(dx,dy),max=43;if(len>max){dx=dx/len*max;dy=dy/len*max}moveX=dx/max;moveY=dy/max;stick.style.transform=`translate(${dx}px,${dy}px)`;}
joy.addEventListener('pointerdown',e=>{joyPointer=e.pointerId;joy.setPointerCapture(e.pointerId);setJoy(e)});
joy.addEventListener('pointermove',e=>{if(e.pointerId===joyPointer)setJoy(e)});
function resetJoy(){joyPointer=null;moveX=moveY=0;stick.style.transform='translate(0,0)'}
joy.addEventListener('pointerup',resetJoy);joy.addEventListener('pointercancel',resetJoy);
renderer.domElement.addEventListener('pointerdown',e=>{if(e.clientX<innerWidth*.38 && e.clientY>innerHeight*.45)return;dragging=true;lastX=e.clientX;lastY=e.clientY;renderer.domElement.setPointerCapture(e.pointerId)});
renderer.domElement.addEventListener('pointermove',e=>{if(dragging){cameraAngle-=(e.clientX-lastX)*.006;lastX=e.clientX;lastY=e.clientY}});
renderer.domElement.addEventListener('pointerup',()=>dragging=false);renderer.domElement.addEventListener('pointercancel',()=>dragging=false);
addEventListener('keydown',e=>{keys[e.key.toLowerCase()]=true;if(e.key==='Shift')sprinting=true});
addEventListener('keyup',e=>{keys[e.key.toLowerCase()]=false;if(e.key==='Shift')sprinting=false});
function animate(){
 requestAnimationFrame(animate);
 const t=performance.now()*.001;
 sun.position.x=Math.sin(t*.025)*25-10;
 const kx=(keys.d||keys.arrowright?1:0)-(keys.a||keys.arrowleft?1:0);
 const ky=(keys.w||keys.arrowup?1:0)-(keys.s||keys.arrowdown?1:0);
 let mx=moveX+kx,my=-moveY+ky;const mag=Math.hypot(mx,my);if(mag>1){mx/=mag;my/=mag}
 const moving=mag>.08;const speed=(sprinting||keys.shift?7.2:4.2);
 if(moving){const forwardX=Math.sin(cameraAngle),forwardZ=Math.cos(cameraAngle);const rightX=Math.cos(cameraAngle),rightZ=-Math.sin(cameraAngle);const dx=(rightX*mx+forwardX*my)*speed/60,dz=(rightZ*mx+forwardZ*my)*speed/60;playerMesh.position.x+=dx;playerMesh.position.z+=dz;playerMesh.rotation.y=Math.atan2(dx,dz);}
 const stride=moving?Math.sin(t*(sprinting||keys.shift?13:9))*.62:0;
 legs[0].rotation.x=stride;legs[1].rotation.x=-stride;arms[0].rotation.x=-stride*.65;arms[1].rotation.x=stride*.65;
 torso.rotation.z=moving?Math.sin(t*5)*.025:0;
 camera.position.x=playerMesh.position.x+Math.sin(cameraAngle)*10;
 camera.position.z=playerMesh.position.z+Math.cos(cameraAngle)*10;
 camera.position.y=5.1;
 camera.lookAt(playerMesh.position.x,1.25,playerMesh.position.z);
 renderer.render(scene,camera);
}
animate();
addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight)});
load();captureIfReturned();
// Monetag In-App Interstitial: frequency 2, 6-minute capping, 30s interval, 5s delay.
if(typeof window.show_12235771==='function'){
 try{window.show_12235771({type:'inApp',inAppSettings:{frequency:2,capping:0.1,interval:30,timeout:5,everyPage:false}}).catch(()=>{});}catch(_e){}
}
</script>
</body></html>"""


@app.get("/")
def index():
    response = make_response(GAME_HTML)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' https://telegram.org https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
        "connect-src 'self' https:; frame-src https:;"
    )
    return response


@app.get("/health")
def health():
    return jsonify(status="ok", game=GAME_TITLE, storage=str(DATA_FILE), time=utc_now())


@app.post("/telegram/webhook")
def telegram_webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return jsonify(error="Unauthorized"), 403
    try:
        update = request.get_json(force=True)
        handle_telegram_update(update)
        return jsonify(ok=True)
    except Exception:
        log.exception("Error procesando actualización de Telegram")
        # Respondemos 200 para evitar reintentos infinitos por errores de lógica.
        return jsonify(ok=False), 200


@app.before_request
def ensure_webhook():
    if request.path != "/telegram/webhook":
        set_telegram_webhook()


if __name__ == "__main__":
    if not BOT_TOKEN:
        log.warning("Configura BOT_TOKEN antes de iniciar el bot")
    if not PUBLIC_URL:
        log.warning("Configura PUBLIC_URL con la URL pública de Render")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
