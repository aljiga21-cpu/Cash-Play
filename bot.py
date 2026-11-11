"""
SUPERVIVENCIA 3D - Telegram Mini App
Backend de un solo archivo: Flask + Telegram Bot API + PayPal Checkout + JSON.

Variables de entorno obligatorias:
  BOT_TOKEN
  PUBLIC_URL                 URL pública de Render, por ejemplo https://mi-juego.onrender.com

Variables opcionales:
  WEBHOOK_SECRET             Secreto aleatorio para proteger el webhook de Telegram
  MONETAG_ZONE_ID            ID de zona Monetag (12235771)
  MONETAG_POSTBACK_SECRET    Secreto para el postback de recompensas Monetag
  ADMIN_SECRET               Secreto para consultar/gestionar retiradas pendientes
  DATA_FILE                  Por defecto ./data/players.json

IMPORTANTE:
- El JSON se guarda en el sistema de archivos local de Render. En un servicio sin disco
  persistente puede perderse al reiniciar, desplegar o mover la instancia.
- Las recompensas Monetag SOLO se abonan mediante el postback del servidor. No confíes
  en una señal JavaScript del navegador como prueba de que se vio un anuncio.
- La tienda web cobra con PayPal Checkout en ventana emergente; el servidor captura y valida cada pedido.
- Telegram Stars queda disponible como ruta heredada, pero la tienda del juego utiliza PayPal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import math
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
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
REWARD_COINS_PER_AD = 10
REWARD_WOOD_PER_AD = 1
REWARD_STONE_PER_AD = 1
REWARD_ENERGY_PER_AD = 5
MAX_REWARDED_ADS_PER_DAY = 10
COINS_PER_CENT = 10000
MIN_WITHDRAW_EUR = 5.00
MIN_WITHDRAW_COINS = int(MIN_WITHDRAW_EUR * 100 * COINS_PER_CENT)
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
        "stars": 100,
    },
    "iron_axe": {
        "title": "Hacha reforzada",
        "description": "Herramienta mejorada para recolectar madera",
        "price": "2.49",
        "upgrade": "iron_axe",
        "stars": 125,
    },
    "vip_7": {
        "title": "Superviviente VIP (7 días)",
        "description": "Bonificación diaria de monedas durante 7 días",
        "price": "3.99",
        "vip_days": 7,
        "stars": 200,
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
        return {"players": {}, "pending_orders": {}, "processed_rewards": [], "withdrawals": []}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("El archivo JSON no contiene un objeto")
        data.setdefault("players", {})
        data.setdefault("pending_orders", {})
        data.setdefault("processed_rewards", [])
        data.setdefault("withdrawals", [])
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
        "redeemable_coins": 0,
        "rewarded_ads_date": "",
        "rewarded_ads_count": 0,
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
        "tools": ["axe", "pickaxe"],
        "world_nodes": [],
        "placed_buildings": [],
        "buildings": {"campfire": 0, "shelter": 0, "cabin": 0},
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
    player.setdefault("redeemable_coins", 0)
    player.setdefault("rewarded_ads_date", "")
    player.setdefault("rewarded_ads_count", 0)
    player.setdefault("tools", ["axe", "pickaxe", "spear"])
    if "spear" not in player["tools"]:
        player["tools"].append("spear")
    player.setdefault("world_nodes", [])
    player.setdefault("placed_buildings", [])
    player.setdefault("buildings", {"campfire": 0, "shelter": 0, "cabin": 0})
    player["buildings"].setdefault("cabin", 0)
    if not player["world_nodes"]:
        rng = random.Random(int(hashlib.sha256(uid.encode()).hexdigest()[:12], 16))
        nodes = []
        for i in range(48):
            a = rng.random() * math.tau
            radius = 8 + rng.random() * 43
            nodes.append({"id": f"tree_{i}", "type": "tree", "x": round(math.cos(a)*radius, 2), "z": round(math.sin(a)*radius, 2), "hp": 3, "max_hp": 3, "active": True})
        for i in range(30):
            a = rng.random() * math.tau
            radius = 6 + rng.random() * 40
            nodes.append({"id": f"rock_{i}", "type": "rock", "x": round(math.cos(a)*radius, 2), "z": round(math.sin(a)*radius, 2), "hp": 2, "max_hp": 2, "active": True})
        player["world_nodes"] = nodes
    # Fauna y peces persistentes: aparecen una sola vez por jugador.
    if not any(n.get("type") in ("animal", "fish") for n in player["world_nodes"]):
        wildlife_rng = random.Random(int(hashlib.sha256((uid + "-wildlife").encode()).hexdigest()[:12], 16))
        for i in range(9):
            a = wildlife_rng.random() * math.tau
            radius = 11 + wildlife_rng.random() * 27
            player["world_nodes"].append({"id": f"animal_{i}", "type": "animal", "species": "deer" if i % 3 else "boar", "x": round(math.cos(a)*radius, 2), "z": round(math.sin(a)*radius, 2), "hp": 2, "max_hp": 2, "active": True})
        # Los peces siguen el cauce sinuoso del río, a lo largo de la isla.
        for i, x in enumerate([-18, -12, -6, 0, 6, 12, 18]):
            z = 6 * math.sin(x * .15) + (0.45 if i % 2 else -0.45)
            player["world_nodes"].append({"id": f"fish_{i}", "type": "fish", "x": x, "z": round(z, 2), "hp": 1, "max_hp": 1, "active": True})
    player["username"] = user.get("username", player.get("username", ""))
    player["first_name"] = user.get("first_name", player.get("first_name", "Superviviente"))
    player["updated_at"] = utc_now()
    return player


def public_player(player: dict) -> dict:
    # No se exponen datos de pago, tokens ni información interna.
    keys = (
        "id", "username", "first_name", "level", "xp", "coins", "redeemable_coins", "rewarded_ads_count", "health",
        "hunger", "thirst", "energy", "wood", "stone", "food", "water",
        "medkits", "inventory", "tools", "buildings", "placed_buildings", "world_nodes", "upgrades", "vip_until",
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
        payload = {"url": f"{PUBLIC_URL}/telegram/webhook", "allowed_updates": ["message", "pre_checkout_query"]}
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


def deliver_paid_product(user_id: str, product_id: str, payment_charge_id: str, payload_id: str) -> dict:
    """Entrega y marca el pedido en una sola escritura; evita duplicados."""
    with store_lock:
        data = load_store()
        processed = data.setdefault("processed_payments", [])
        if payment_charge_id in processed:
            player = data["players"].get(user_id)
            return public_player(player) if player else {}
        player = data["players"].get(user_id)
        product = PRODUCTS.get(product_id)
        order = data.setdefault("pending_orders", {}).get(payload_id)
        if not player or not product or not order:
            raise ValueError("Jugador, pedido o producto de pago no válido")
        if str(order.get("user_id")) != user_id or order.get("product_id") != product_id:
            raise ValueError("El pedido no pertenece a este jugador")
        if order.get("status") not in ("CREATED", "PAYMENT_RECEIVED"):
            raise ValueError("Este pedido ya ha sido procesado")
        player["coins"] = int(player.get("coins", 0)) + int(product.get("coins", 0))
        player["medkits"] = int(player.get("medkits", 0)) + int(product.get("medkits", 0))
        upgrade = product.get("upgrade")
        if upgrade and upgrade not in player.setdefault("upgrades", []):
            player["upgrades"].append(upgrade)
        if product.get("vip_days"):
            player["vip_until"] = max(int(player.get("vip_until", 0)), int(time.time())) + int(product["vip_days"]) * 86400
        player["updated_at"] = utc_now()
        processed.append(payment_charge_id)
        data["processed_payments"] = processed[-5000:]
        order["status"] = "COMPLETED"
        order["completed_at"] = utc_now()
        order["telegram_payment_charge_id"] = payment_charge_id
        save_store(data)
        return public_player(player)


def handle_telegram_update(update: dict) -> None:
    # Telegram exige responder a la preconfirmación antes de entregar un producto digital.
    pre = update.get("pre_checkout_query")
    if pre:
        payload = str(pre.get("invoice_payload", ""))
        with store_lock:
            data = load_store()
            order = data.setdefault("pending_orders", {}).get(payload)
            valid = bool(order and order.get("status") == "CREATED"
                         and str(order.get("user_id")) == str(pre.get("from", {}).get("id"))
                         and pre.get("currency") == "XTR"
                         and int(pre.get("total_amount", 0)) == int(order.get("stars", -1)))
        telegram_api("answerPreCheckoutQuery", {
            "pre_checkout_query_id": pre["id"],
            "ok": valid,
            **({} if valid else {"error_message": "No se ha podido validar esta compra. Vuelve a abrir la tienda."})
        })
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    payment = message.get("successful_payment")
    if payment:
        payload = str(payment.get("invoice_payload", ""))
        charge_id = str(payment.get("telegram_payment_charge_id", ""))
        payer_id = str(message.get("from", {}).get("id", ""))
        with store_lock:
            data = load_store()
            order = data.setdefault("pending_orders", {}).get(payload)
            valid = bool(order and order.get("status") == "CREATED"
                         and str(order.get("user_id")) == payer_id
                         and payment.get("currency") == "XTR"
                         and int(payment.get("total_amount", 0)) == int(order.get("stars", -1)))
        if valid and charge_id:
            product_id = order["product_id"]
            deliver_paid_product(payer_id, product_id, charge_id, payload)
            send_message(message.get("chat", {}).get("id"), "✅ ¡Compra completada! Tu producto ya está disponible en ISLA SALVAJE.")
        else:
            log.warning("Pago de Telegram no válido o sin charge_id; payload=%s", payload)
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


# ----------------------------- PAGOS TELEGRAM STARS -----------------------------

@app.post("/api/stars/create-invoice")
def stars_create_invoice():
    try:
        user = authenticated_user()
        body = request.get_json(silent=True) or {}
        product_id = str(body.get("product_id", ""))
        product = PRODUCTS.get(product_id)
        if not product or int(product.get("stars", 0)) <= 0:
            return jsonify(error="Producto no válido"), 400
        payload_id = "isla_" + uuid.uuid4().hex
        stars = int(product["stars"])
        with store_lock:
            data = load_store()
            get_or_create_player(data, user)
            data.setdefault("pending_orders", {})[payload_id] = {
                "user_id": str(user["id"]),
                "product_id": product_id,
                "stars": stars,
                "status": "CREATED",
                "created_at": utc_now(),
                "payload": payload_id,
            }
            save_store(data)
        invoice = telegram_api("createInvoiceLink", {
            "title": product["title"][:32],
            "description": product["description"][:255],
            "payload": payload_id,
            "provider_token": "",
            "currency": "XTR",
            "prices": [{"label": product["title"][:32], "amount": stars}],
        })
        return jsonify(ok=True, invoice_url=invoice["result"], stars=stars)
    except Exception as exc:
        log.exception("No se pudo crear la factura de Telegram Stars")
        return jsonify(error="No se ha podido iniciar el pago con Telegram Stars", detail=str(exc)), 400


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
        expected_product = PRODUCTS[pending["product_id"]]
        units = captured.get("purchase_units", [])
        valid_unit = any(
            unit.get("reference_id") == pending["product_id"]
            and str(unit.get("custom_id")) == str(user["id"])
            and unit.get("amount", {}).get("currency_code") == "EUR"
            and str(unit.get("amount", {}).get("value")) == str(expected_product["price"])
            for unit in units
        )
        if not valid_unit:
            log.error("Datos de importe/moneda/usuario no coinciden en pedido PayPal %s", order_id)
            return jsonify(error="Los datos del pago no coinciden con el pedido"), 402

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
            today = datetime.now(timezone.utc).date().isoformat()
            if player.get("rewarded_ads_date") != today:
                player["rewarded_ads_date"] = today
                player["rewarded_ads_count"] = 0
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

            if action == "harvest_node":
                node_id = str(body.get("node_id", ""))
                px, pz = float(body.get("x", 9999)), float(body.get("z", 9999))
                node = next((n for n in player["world_nodes"] if n["id"] == node_id and n.get("active")), None)
                if not node:
                    return jsonify(error="Este recurso ya se ha agotado"), 400
                if math.hypot(px-node["x"], pz-node["z"]) > 3.8:
                    return jsonify(error="Acércate más al recurso para trabajar"), 400
                kind = node["type"]
                needed = "axe" if kind == "tree" else "pickaxe" if kind == "rock" else "spear"
                if needed not in player.get("tools", []):
                    label = {"axe": "un hacha", "pickaxe": "un pico", "spear": "una lanza"}[needed]
                    return jsonify(error="Necesitas " + label), 400
                cost = 4 if kind == "tree" else 5 if kind == "rock" else 7 if kind == "animal" else 3
                if player["energy"] < cost:
                    return jsonify(error="No tienes energía suficiente"), 400
                node["hp"] -= 1
                player["energy"] -= cost
                if node["hp"] <= 0:
                    node["active"] = False
                    if node["type"] == "tree":
                        amount = 4 if "iron_axe" in player["upgrades"] else 3
                        player["wood"] += amount
                        apply_xp(player, 12)
                        message = f"Árbol talado. Has conseguido {amount} de madera."
                    elif kind == "rock":
                        player["stone"] += 3
                        apply_xp(player, 12)
                        message = "Roca destruida. Has conseguido 3 piedras."
                    elif kind == "fish":
                        player["food"] += 1
                        apply_xp(player, 10)
                        message = "¡Pescado capturado! Has conseguido 1 comida."
                    else:
                        meat = 3 if node.get("species") == "deer" else 2
                        player["food"] += meat
                        apply_xp(player, 18)
                        message = f"Animal cazado. Has conseguido {meat} raciones de carne."
                else:
                    message = f"Golpe realizado. Quedan {node['hp']} golpes."
            elif action == "build_cabin":
                px, pz = float(body.get("x", 0)), float(body.get("z", 0))
                if player["wood"] < 20 or player["stone"] < 8:
                    return jsonify(error="Necesitas 20 maderas y 8 piedras para construir una cabaña"), 400
                if any(math.hypot(float(b.get("x",0))-px, float(b.get("z",0))-pz) < 7 for b in player["placed_buildings"]):
                    return jsonify(error="No hay espacio suficiente para construir aquí"), 400
                player["wood"] -= 20
                player["stone"] -= 8
                player["placed_buildings"].append({"id": str(uuid.uuid4()), "type": "cabin", "x": px, "z": pz, "built_at": utc_now()})
                player["buildings"]["cabin"] = player["buildings"].get("cabin", 0) + 1
                apply_xp(player, 80)
                message = "¡Cabaña construida! Ya está guardada en tu isla."
            elif action == "build_campfire":
                if player["wood"] < 5 or player["stone"] < 2:
                    return jsonify(error="Necesitas 5 madera y 2 piedras"), 400
                player["wood"] -= 5
                player["stone"] -= 2
                player["buildings"]["campfire"] += 1
                player["placed_buildings"].append({"id": str(uuid.uuid4()), "type": "campfire", "x": float(body.get("x", 0)), "z": float(body.get("z", 0)), "built_at": utc_now()})
                apply_xp(player, 25)
                message = "Has construido una hoguera."
            elif action == "build_shelter":
                if player["wood"] < 12 or player["stone"] < 5:
                    return jsonify(error="Necesitas 12 madera y 5 piedras"), 400
                player["wood"] -= 12
                player["stone"] -= 5
                player["buildings"]["shelter"] += 1
                player["placed_buildings"].append({"id": str(uuid.uuid4()), "type": "shelter", "x": float(body.get("x", 0)), "z": float(body.get("z", 0)), "built_at": utc_now()})
                apply_xp(player, 50)
                message = "Has construido un refugio."
            elif action == "eat":
                if player["food"] < 1:
                    return jsonify(error="No tienes comida"), 400
                player["food"] -= 1
                player["hunger"] = min(100, player["hunger"] + 35)
                player["health"] = min(100, player["health"] + 3)
                message = "Has comido."
            elif action == "drink_river":
                px, pz = float(body.get("x", 9999)), float(body.get("z", 9999))
                river_z = 6 * math.sin(px * .15)
                if abs(pz - river_z) > 3.2 or abs(px) > 22:
                    return jsonify(error="Acércate a la orilla del río para beber"), 400
                player["thirst"] = min(100, player["thirst"] + 30)
                message = "Has bebido agua del río. Recuerda que en la isla el agua puede necesitar hervirse."
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
    """Postback servidor-a-servidor. Configura Monetag para enviar secret, user_id y tx_id."""
    if not MONETAG_POSTBACK_SECRET:
        return jsonify(error="Postback no configurado"), 503
    secret = request.args.get("secret", "")
    if not hmac.compare_digest(secret, MONETAG_POSTBACK_SECRET):
        return jsonify(error="No autorizado"), 403

    user_id = str(request.args.get("user_id", "")).strip()
    transaction_id = str(request.args.get("tx_id", "")).strip()
    if not user_id or not transaction_id or len(transaction_id) > 200:
        return jsonify(error="Faltan user_id o tx_id válidos"), 400

    today = datetime.now(timezone.utc).date().isoformat()
    with store_lock:
        data = load_store()
        processed = data.setdefault("processed_rewards", [])
        if transaction_id in processed:
            return jsonify(ok=True, duplicate=True)
        player = data["players"].get(user_id)
        if not player:
            return jsonify(error="Jugador no encontrado"), 404
        if player.get("rewarded_ads_date") != today:
            player["rewarded_ads_date"] = today
            player["rewarded_ads_count"] = 0
        count = int(player.get("rewarded_ads_count", 0))
        if count >= MAX_REWARDED_ADS_PER_DAY:
            return jsonify(error="Límite diario de anuncios recompensados alcanzado"), 429

        # Monedas normales para jugar + saldo separado que sí puede solicitarse para canje.
        player["coins"] = int(player.get("coins", 0)) + REWARD_COINS_PER_AD
        player["redeemable_coins"] = int(player.get("redeemable_coins", 0)) + REWARD_COINS_PER_AD
        player["wood"] = int(player.get("wood", 0)) + REWARD_WOOD_PER_AD
        player["stone"] = int(player.get("stone", 0)) + REWARD_STONE_PER_AD
        player["energy"] = min(100, int(player.get("energy", 0)) + REWARD_ENERGY_PER_AD)
        player["rewarded_ads_count"] = count + 1
        player["updated_at"] = utc_now()
        processed.append(transaction_id)
        # Conserva un historial amplio para evitar dobles abonos por reintentos del proveedor.
        processed[:] = processed[-50000:]
        save_store(data)
    return jsonify(ok=True, coins=REWARD_COINS_PER_AD, wood=REWARD_WOOD_PER_AD,
                   stone=REWARD_STONE_PER_AD, energy=REWARD_ENERGY_PER_AD,
                   ads_today=count + 1, ads_remaining=MAX_REWARDED_ADS_PER_DAY-count-1)


@app.post("/api/wallet/withdraw")
def wallet_withdraw():
    """Reserva saldo canjeable y crea una solicitud pendiente de pago manual por PayPal."""
    try:
        user = authenticated_user()
        body = request.get_json(silent=True) or {}
        email = str(body.get("paypal_email", "")).strip().lower()
        if len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            return jsonify(error="Introduce un correo PayPal válido"), 400
        with store_lock:
            data = load_store()
            player = data["players"].get(str(user["id"]))
            if not player:
                return jsonify(error="Jugador no encontrado"), 404
            balance = int(player.get("redeemable_coins", 0))
            if balance < MIN_WITHDRAW_COINS:
                return jsonify(error=f"Necesitas {MIN_WITHDRAW_COINS:,} monedas canjeables para retirar 5 €"), 400
            # Se reserva exactamente el mínimo; el resto permanece en la cartera.
            amount_coins = MIN_WITHDRAW_COINS
            player["redeemable_coins"] = balance - amount_coins
            withdrawal = {
                "id": "wd_" + uuid.uuid4().hex,
                "user_id": str(user["id"]),
                "paypal_email": email,
                "coins": amount_coins,
                "amount_eur": MIN_WITHDRAW_EUR,
                "status": "PENDING",
                "created_at": utc_now(),
            }
            data.setdefault("withdrawals", []).append(withdrawal)
            save_store(data)
        return jsonify(ok=True, message="Solicitud registrada. El pago queda pendiente de revisión y envío manual por PayPal.",
                       withdrawal={"id": withdrawal["id"], "amount_eur": MIN_WITHDRAW_EUR, "status": "PENDING"})
    except Exception as exc:
        log.exception("Error creando solicitud de retirada")
        return jsonify(error=str(exc)), 400


@app.route("/api/admin/withdrawals", methods=["GET", "POST"])
def admin_withdrawals():
    if not ADMIN_SECRET:
        return jsonify(error="ADMIN_SECRET no configurado"), 503
    supplied = request.headers.get("X-Admin-Secret", "")
    if not hmac.compare_digest(supplied, ADMIN_SECRET):
        return jsonify(error="No autorizado"), 403
    with store_lock:
        data = load_store()
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            wid = str(body.get("withdrawal_id", ""))
            status = str(body.get("status", ""))
            if status not in ("PAID", "REJECTED"):
                return jsonify(error="Estado no válido"), 400
            item = next((x for x in data.setdefault("withdrawals", []) if x.get("id") == wid), None)
            if not item or item.get("status") != "PENDING":
                return jsonify(error="Solicitud no encontrada o ya procesada"), 404
            item["status"] = status
            item["processed_at"] = utc_now()
            item["paypal_transaction_id"] = str(body.get("paypal_transaction_id", ""))[:200]
            if status == "REJECTED":
                player = data["players"].get(item["user_id"])
                if player:
                    player["redeemable_coins"] = int(player.get("redeemable_coins", 0)) + int(item["coins"])
            save_store(data)
        pending = [{k: v for k, v in item.items() if k != "user_id"} | {"user_id": item.get("user_id")} for item in data.setdefault("withdrawals", []) if item.get("status") == "PENDING"]
        return jsonify(ok=True, pending=pending)


# ----------------------------- INTERFAZ 3D -----------------------------

GAME_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#101b19">
<title>ISLA SALVAJE - Supervivencia 3D</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script async src="https://libtl.com/sdk.js" data-zone="12235771" data-sdk="show_12235771"></script>
<script>window.ISLA_PAYPAL_CLIENT_ID="__PAYPAL_CLIENT_ID__";</script>
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
#walletPanel{display:none;margin-top:12px;padding:13px;border:1px solid #ffffff25;border-radius:14px;background:#0b1713}#walletPanel input{width:100%;padding:12px;border-radius:9px;border:1px solid #ffffff35;background:#14251f;color:#fff;margin:6px 0}#walletPanel .walletBig{font-size:24px;font-weight:900;color:#9be4ad}#walletPanel .walletNote{font-size:11px;color:#b9c9c0;line-height:1.45}
#paypalPanel{display:none;margin-top:14px;padding:13px;border:1px solid #ffffff25;border-radius:14px;background:#0b1713}#paypal-button-container{min-height:48px}#paypalStatus{font-size:12px;color:#c9d8cf;margin:8px 0}
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
 <button onclick="harvestNearest()">🪓 Recolectar / cazar / pescar</button>
 <button onclick="buildCabin()">🏠 Construir cabaña</button>
 <button onclick="act('rest')">⚡ Descansar</button>
 <button onclick="act('eat')">🍎 Comer</button>
 <button onclick="act('drink')">💧 Beber cantimplora</button>
 <button onclick="act('drink_river',{x:playerMesh.position.x,z:playerMesh.position.z})">🏞️ Beber del río</button>
 <button onclick="act('daily')">🎁 Premio diario</button>
 <button onclick="act('build_campfire')">🔥 Hoguera</button>
 <button onclick="act('build_shelter')">🏕️ Refugio</button>
 <button id="rewardedAdButton" onclick="showRewardedAd()">🎬 Anuncio + recompensa</button>
 <button onclick="showPopupAd()">📺 Anuncio extra</button>
 <button onclick="openWallet()">💶 Mi cartera</button>
 <button onclick="openShop()">🛒 Tienda</button>
</div>
<div id="walletPanel" class="sheet" style="position:fixed;z-index:7;left:0;right:0;bottom:0;max-height:75vh;overflow:auto">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3>💶 Mi cartera</h3><button onclick="closeWallet()">Cerrar ✕</button></div>
 <div class="walletBig"><span id="redeemableCoins">0</span> monedas canjeables</div>
 <p id="walletEuro" class="walletNote">Equivalencia: 0,00 €</p>
 <p id="walletAds" class="walletNote">Anuncios recompensados hoy: 0/10</p>
 <p class="walletNote">Conversión: 10.000 monedas = 0,01 €. Retirada mínima: 5 € (5.000.000 monedas). Con 10 monedas por anuncio y 10 anuncios diarios, tardarías unos 137 años en alcanzar 5 €; esta escala debe revisarse antes de prometer pagos. Las recompensas requieren confirmación del postback de Monetag. Los pagos se revisan y envían manualmente por PayPal.</p>
 <input id="paypalEmail" type="email" autocomplete="email" placeholder="Tu correo de PayPal">
 <button class="primary" style="width:100%" onclick="requestWithdrawal()">Solicitar retirada de 5 €</button>
 <p id="walletStatus" class="walletNote"></p>
 </div>
<div id="shop"><div class="sheet">
 <div style="display:flex;justify-content:space-between;align-items:center"><h3>🛒 Tienda de supervivencia</h3><button onclick="closeShop()">Cerrar ✕</button></div>
 <div class="item"><div><b>Pack de superviviente</b><small>500 monedas + 3 botiquines · 1,99 €</small></div><button onclick="buy('starter_pack')">Comprar</button></div>
 <div class="item"><div><b>Hacha reforzada</b><small>Mejora para recolectar más madera · 2,49 €</small></div><button onclick="buy('iron_axe')">Comprar</button></div>
 <div class="item"><div><b>VIP 7 días</b><small>Ventajas VIP · 3,99 €</small></div><button onclick="buy('vip_7')">Comprar</button></div>
 <div id="paypalPanel"><div style="display:flex;justify-content:space-between;align-items:center"><b>🔒 Pago seguro con PayPal</b><button onclick="closePaypal()">Cerrar</button></div><p id="paypalStatus">Elige PayPal para completar tu compra.</p><div id="paypal-button-container"></div></div>
 <p class="sub">El pago se realiza con PayPal. Al terminar, volverás a ISLA SALVAJE y recibirás tu compra automáticamente.</p>
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
 const redeemable=Number(p.redeemable_coins||0);
 const walletCoins=document.getElementById('redeemableCoins'); if(walletCoins) walletCoins.textContent=redeemable.toLocaleString('es-ES');
 const walletEuro=document.getElementById('walletEuro'); if(walletEuro) walletEuro.textContent='Equivalencia: '+(redeemable/1000000).toLocaleString('es-ES',{style:'currency',currency:'EUR'});
 const walletAds=document.getElementById('walletAds'); if(walletAds) walletAds.textContent='Anuncios recompensados hoy: '+Number(p.rewarded_ads_count||0)+'/10';
 const rewardBtn=document.getElementById('rewardedAdButton'); if(rewardBtn){const capped=Number(p.rewarded_ads_count||0)>=10;rewardBtn.disabled=capped;rewardBtn.textContent=capped?'Límite diario alcanzado':'🎬 Anuncio + recompensa';}
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
 try{const j=await api('/api/me');renderPlayer(j.player);syncWorld(j.player)}catch(e){toast(e.message)}
}
window.act=async(action,extra={})=>{try{const j=await api('/api/action',{action,...extra});renderPlayer(j.player);syncWorld(j.player);toast(j.message);return j}catch(e){toast(e.message);return null}};
window.harvestNearest=async()=>{
 const n=nearestResource(); if(!n){toast('No hay recursos, animales ni peces cerca. Explora la isla.');return;}
 await playHarvestAnimation(n);
 await window.act('harvest_node',{node_id:n.userData.node.id,x:playerMesh.position.x,z:playerMesh.position.z});
};
window.buildCabin=async()=>{const j=await window.act('build_cabin',{x:playerMesh.position.x,z:playerMesh.position.z});if(j)animateBuild(j.player.placed_buildings.at(-1));};
window.openShop=()=>document.getElementById('shop').style.display='flex';
window.openWallet=()=>{document.getElementById('walletPanel').style.display='block';if(player)renderPlayer(player)};
window.closeWallet=()=>document.getElementById('walletPanel').style.display='none';
window.requestWithdrawal=async()=>{
 const status=document.getElementById('walletStatus');
 try{status.textContent='Registrando solicitud…';const j=await api('/api/wallet/withdraw',{paypal_email:document.getElementById('paypalEmail').value});status.textContent=j.message;await load();}
 catch(e){status.textContent=e.message;}
};
window.closeShop=()=>document.getElementById('shop').style.display='none';
let paypalSdkPromise=null;
let paypalButtonsInstance=null;
let selectedPaypalProduct=null;
function loadPaypalSdk(){
 const clientId=window.ISLA_PAYPAL_CLIENT_ID||'';
 if(!clientId) return Promise.reject(new Error('Falta PAYPAL_CLIENT_ID en las variables de Render.'));
 if(window.paypal?.Buttons) return Promise.resolve(window.paypal);
 if(paypalSdkPromise) return paypalSdkPromise;
 paypalSdkPromise=new Promise((resolve,reject)=>{
  const script=document.createElement('script');
  script.src='https://www.paypal.com/sdk/js?client-id='+encodeURIComponent(clientId)+'&currency=EUR&intent=capture&components=buttons';
  script.onload=()=>window.paypal?.Buttons?resolve(window.paypal):reject(new Error('No se ha podido iniciar PayPal.'));
  script.onerror=()=>reject(new Error('No se ha podido cargar PayPal. Comprueba la conexión.'));
  document.head.appendChild(script);
 });
 return paypalSdkPromise;
}
window.closePaypal=()=>{document.getElementById('paypalPanel').style.display='none';selectedPaypalProduct=null;};
window.buy=async(product_id)=>{
 try{
  selectedPaypalProduct=product_id;
  const panel=document.getElementById('paypalPanel');
  const status=document.getElementById('paypalStatus');
  panel.style.display='block';status.textContent='Cargando pago seguro…';
  await loadPaypalSdk();
  const container=document.getElementById('paypal-button-container');
  container.innerHTML='';
  if(paypalButtonsInstance){paypalButtonsInstance=null;}
  paypalButtonsInstance=window.paypal.Buttons({
   style:{layout:'vertical',shape:'rect',label:'paypal',height:45},
   createOrder:async()=>{
    const result=await api('/api/paypal/create-order',{product_id:selectedPaypalProduct});
    return result.order_id;
   },
   onApprove:async(data)=>{
    status.textContent='Verificando el pago con PayPal…';
    try{
     const result=await api('/api/paypal/capture-order',{order_id:data.orderID});
     if(!result.ok) throw new Error('PayPal no ha confirmado la compra.');
     renderPlayer(result.player);syncWorld(result.player);
     status.textContent='¡Pago confirmado! Tu compra ya está en tu inventario.';
     toast('✅ Compra recibida. ¡Gracias por apoyar ISLA SALVAJE!');
     setTimeout(()=>{panel.style.display='none';},1800);
    }catch(err){status.textContent=err.message||'No se pudo verificar el pago. Contacta con soporte si PayPal te ha cobrado.';}
   },
   onCancel:()=>{status.textContent='Has cancelado el pago. Tu partida sigue abierta.';},
   onError:(err)=>{console.error('PayPal checkout:',err);status.textContent='PayPal ha encontrado un error. Inténtalo de nuevo.';}
  });
  await paypalButtonsInstance.render('#paypal-button-container');
  status.textContent='Pulsa el botón PayPal para pagar sin cerrar tu partida.';
 }catch(e){document.getElementById('paypalStatus').textContent=e.message;toast(e.message);}
};
// Monetag SDK: esperamos a que el script asíncrono esté realmente disponible.
// La recompensa económica se acredita exclusivamente mediante postback S2S.
function waitForMonetag(timeoutMs=15000){
 return new Promise((resolve,reject)=>{
  if(typeof window.show_12235771==='function') return resolve(window.show_12235771);
  const started=Date.now();
  const timer=setInterval(()=>{
   if(typeof window.show_12235771==='function'){clearInterval(timer);resolve(window.show_12235771);}
   else if(Date.now()-started>=timeoutMs){clearInterval(timer);reject(new Error('Monetag no ha cargado. Comprueba el bloqueador de anuncios, la conexión y que la zona 12235771 esté activa.'));}
  },250);
 });
}
window.showRewardedAd=async()=>{
 if(player && Number(player.rewarded_ads_count||0)>=10){toast('Has alcanzado los 10 anuncios recompensados de hoy.');return;}
 try{
  const showAd=await waitForMonetag();
  await Promise.resolve(showAd());
  toast('Anuncio terminado. La recompensa llegará cuando Monetag confirme el postback.');
  setTimeout(()=>load(),4000);
 }catch(e){console.warn('Monetag rewarded:',e);toast('No se pudo mostrar el anuncio: '+(e?.message||'error'));}
};
window.showPopupAd=async()=>{
 if(player && Number(player.rewarded_ads_count||0)>=10){toast('Has alcanzado los 10 anuncios recompensados de hoy.');return;}
 try{
  const showAd=await waitForMonetag();
  await Promise.resolve(showAd('pop'));
  toast('Anuncio solicitado. La recompensa depende de la confirmación de Monetag.');
  setTimeout(()=>load(),4000);
 }catch(e){console.warn('Monetag popup:',e);toast('No se pudo mostrar el anuncio: '+(e?.message||'error'));}
};
async function startMonetagInApp(){
 try{
  const showAd=await waitForMonetag();
  // Ajustes facilitados: 2 anuncios, capping 0.1h (6 min), 30s de intervalo,
  // 5s de espera inicial y no reiniciar sesión al cambiar de página.
  await Promise.resolve(showAd({type:'inApp',inAppSettings:{frequency:2,capping:0.1,interval:30,timeout:5,everyPage:false}}));
  console.info('Monetag In-App Interstitial iniciado.');
 }catch(e){console.warn('Monetag In-App Interstitial:',e);}
}
async function captureIfReturned(){ /* PayPal captura el pedido desde onApprove; la partida permanece abierta. */ }

// Escena 3D mejorada: vegetación animada, modelos orgánicos y acciones físicas.
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
const ground=new THREE.Mesh(new THREE.PlaneGeometry(220,220,90,90),new THREE.MeshStandardMaterial({color:0x496c43,roughness:1}));
// Ondulación suave del terreno para evitar una superficie completamente plana.
const gp=ground.geometry.attributes.position;for(let i=0;i<gp.count;i++){const x=gp.getX(i),z=gp.getY(i);gp.setZ(i,Math.sin(x*.17)*.10+Math.cos(z*.21)*.08+Math.sin((x+z)*.08)*.06);}ground.geometry.computeVertexNormals();ground.rotation.x=-Math.PI/2;ground.receiveShadow=true;scene.add(ground);
// Hierba en instancias: muchas briznas con movimiento de viento en el shader.
const grassMat=new THREE.MeshStandardMaterial({color:0x65934b,roughness:1,side:THREE.DoubleSide});
grassMat.onBeforeCompile=(shader)=>{shader.vertexShader=shader.vertexShader.replace('#include <common>','#include <common>\nuniform float uWindTime;');shader.vertexShader=shader.vertexShader.replace('#include <begin_vertex>','#include <begin_vertex>\ntransformed.x += sin(uWindTime + instanceMatrix[3].x*0.17 + instanceMatrix[3].z*0.13) * position.y * 0.10;');shader.uniforms.uWindTime={value:0};grassMat.userData.shader=shader;};
const bladeGeo=new THREE.ConeGeometry(.045,.42,3);bladeGeo.translate(0,.21,0);const grass=new THREE.InstancedMesh(bladeGeo,grassMat,1800);const dummy=new THREE.Object3D();let gi=0;for(let i=0;i<1800;i++){const x=(Math.random()-.5)*42,z=(Math.random()-.5)*42;if(Math.hypot(x/1.25,z/.85)>20)continue;dummy.position.set(x,.05,z);dummy.rotation.y=Math.random()*Math.PI;dummy.scale.setScalar(.55+Math.random()*.8);dummy.updateMatrix();grass.setMatrixAt(gi++,dummy.matrix);}grass.count=gi;grass.instanceMatrix.needsUpdate=true;scene.add(grass);
const sand=new THREE.Mesh(new THREE.CircleGeometry(22,64),new THREE.MeshStandardMaterial({color:0xc5b27d,roughness:1}));sand.rotation.x=-Math.PI/2;sand.position.y=.025;sand.scale.set(1.25,.85,1);scene.add(sand);
const water=new THREE.Mesh(new THREE.PlaneGeometry(220,220),new THREE.MeshStandardMaterial({color:0x287f91,roughness:.35,metalness:.1}));water.rotation.x=-Math.PI/2;water.position.y=-.12;scene.add(water);
// Río sinuoso con orillas de arena, agua animada y reflejos suaves.
const riverPoints=[];for(let i=0;i<=80;i++){const x=-23+i*46/80;riverPoints.push(new THREE.Vector3(x,.055,6*Math.sin(x*.15)));}
const riverCurve=new THREE.CatmullRomCurve3(riverPoints);
const riverBed=new THREE.Mesh(new THREE.TubeGeometry(riverCurve,160,2.15,10,false),new THREE.MeshStandardMaterial({color:0x9a9a70,roughness:1}));riverBed.position.y=-.02;scene.add(riverBed);
const river=new THREE.Mesh(new THREE.TubeGeometry(riverCurve,180,1.72,16,false),new THREE.MeshPhysicalMaterial({color:0x238da5,roughness:.18,metalness:.05,transparent:true,opacity:.9,clearcoat:.8,clearcoatRoughness:.12}));river.position.y=.025;river.userData.isRiver=true;scene.add(river);
const riverBanks=new THREE.Group();for(let side of [-1,1]){const bank=new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(riverPoints.map(v=>new THREE.Vector3(v.x,.04,v.z+side*2.05))),160,.28,6,false),new THREE.MeshStandardMaterial({color:0xb6a477,roughness:1}));riverBanks.add(bank);}scene.add(riverBanks);
const riverFishMeshes=new Map();const fishSwimmers=[];
function makeFish(node){const g=new THREE.Group();g.position.set(node.x,.22,node.z);const body=new THREE.Mesh(new THREE.SphereGeometry(.22,12,8),new THREE.MeshStandardMaterial({color:0xd9a85b,metalness:.25,roughness:.35}));body.scale.set(1.45,.55,.65);g.add(body);const tail=new THREE.Mesh(new THREE.ConeGeometry(.13,.25,5),new THREE.MeshStandardMaterial({color:0xc98543}));tail.rotation.z=-Math.PI/2;tail.position.x=-.34;g.add(tail);g.userData.node=node;scene.add(g);riverFishMeshes.set(node.id,g);fishSwimmers.push(g);return g;}
const trunkMat=new THREE.MeshStandardMaterial({color:0x65452d,roughness:1}),leafMat=new THREE.MeshStandardMaterial({color:0x285b38,roughness:1}),leafLightMat=new THREE.MeshStandardMaterial({color:0x3d7844,roughness:1});
const animatedTrees=[];
const resourceMeshes=new Map();const fallingTrees=[];let harvestAnimating=false;
function tree(x,z,s=1,node){
 const g=new THREE.Group();g.position.set(x,0,z);g.scale.setScalar(s);
 const trunk=new THREE.Mesh(new THREE.CylinderGeometry(.20,.34,2.65,10),trunkMat);trunk.position.y=1.32;trunk.castShadow=true;trunk.receiveShadow=true;g.add(trunk);
 // Raíces visibles y ramas laterales.
 for(let i=0;i<4;i++){const root=new THREE.Mesh(new THREE.CylinderGeometry(.045,.13,.62,6),trunkMat);root.position.set(Math.cos(i*Math.PI/2)*.22,.16,Math.sin(i*Math.PI/2)*.22);root.rotation.z=Math.cos(i*Math.PI/2)*.45;root.rotation.x=Math.sin(i*Math.PI/2)*.45;g.add(root);}
 for(let i=0;i<5;i++){const branch=new THREE.Mesh(new THREE.CylinderGeometry(.035,.09,.9,6),trunkMat);branch.position.set(Math.sin(i*2.2)*.45,1.55+i*.18,Math.cos(i*2.2)*.4);branch.rotation.z=.8*Math.cos(i*2.2);branch.rotation.x=.8*Math.sin(i*2.2);g.add(branch);}
 for(let i=0;i<7;i++){const crown=new THREE.Mesh(new THREE.IcosahedronGeometry(.92+Math.random()*.18,1),i%3===0?leafLightMat:leafMat);crown.position.set(Math.sin(i*2.4)*.48,2.45+(i%3)*.55,Math.cos(i*2.4)*.42);crown.scale.set(1.12,.9,1);crown.castShadow=true;g.add(crown);}
 g.userData.node=node;g.userData.trunk=trunk;g.userData.baseRotation=0;scene.add(g);resourceMeshes.set(node.id,g);animatedTrees.push(g);return g;
}
const rockMat=new THREE.MeshStandardMaterial({color:0x777b76,roughness:1});
function createRock(node){const g=new THREE.Group();g.position.set(node.x,0,node.z);const count=3+Math.floor(Math.random()*3);for(let i=0;i<count;i++){const rock=new THREE.Mesh(new THREE.DodecahedronGeometry(.48+Math.random()*.34,1),new THREE.MeshStandardMaterial({color:new THREE.Color().setHSL(.55+Math.random()*.03,.035,.39+Math.random()*.12),roughness:1}));rock.position.set((Math.random()-.5)*.65,.25+Math.random()*.18,(Math.random()-.5)*.55);rock.rotation.set(Math.random(),Math.random()*3,Math.random());rock.scale.set(1,.65+Math.random()*.35,.9);rock.castShadow=true;rock.receiveShadow=true;g.add(rock);}g.userData.node=node;scene.add(g);resourceMeshes.set(node.id,g);}

function createAnimal(node){
 const g=new THREE.Group();g.position.set(node.x,0,node.z);const fur=new THREE.MeshStandardMaterial({color:node.species==='boar'?0x594334:0x9b7048,roughness:1});const dark=new THREE.MeshStandardMaterial({color:0x33251d,roughness:1});
 const body=new THREE.Mesh(new THREE.SphereGeometry(.58,16,12),fur);body.position.y=.82;body.scale.set(1.15,.78,.72);body.castShadow=true;g.add(body);
 const head=new THREE.Mesh(new THREE.SphereGeometry(.32,14,10),fur);head.position.set(0,1.12,.58);head.scale.set(.85,.9,.9);head.castShadow=true;g.add(head);
 for(const side of [-1,1]){const leg=new THREE.Mesh(new THREE.CylinderGeometry(.075,.09,.62,8),dark);leg.position.set(side*.3,.34,.05);g.add(leg);const ear=new THREE.Mesh(new THREE.ConeGeometry(.11,.28,6),fur);ear.position.set(side*.2,1.43,.55);ear.rotation.x=-.25;g.add(ear);}
 if(node.species==='deer'){for(const side of [-1,1]){const antler=new THREE.Mesh(new THREE.CylinderGeometry(.025,.045,.55,6),dark);antler.position.set(side*.19,1.55,.58);antler.rotation.z=side*.28;g.add(antler);}}
 g.userData.node=node;g.userData.baseY=0;scene.add(g);resourceMeshes.set(node.id,g);return g;
}
function syncWorld(p){
 if(!p)return;
 for(const n of (p.world_nodes||[])){
  const existing=resourceMeshes.get(n.id);
  if(!n.active){if(existing){if(n.type==='tree'){fallingTrees.push({mesh:existing,life:1.25,dir:Math.random()>.5?1:-1});}else scene.remove(existing);resourceMeshes.delete(n.id);riverFishMeshes.delete(n.id)}continue;}
  if(!existing){if(n.type==='tree')tree(n.x,n.z,.8,n);else if(n.type==='rock')createRock(n);else if(n.type==='animal')createAnimal(n);else if(n.type==='fish')makeFish(n);}
  else existing.userData.node=n;
 }
 for(const b of (p.placed_buildings||[])) if(!placedIds.has(b.id)){animateBuild(b);}
}
function nearestResource(){let best=null,dist=4.2;for(const obj of resourceMeshes.values()){const d=Math.hypot(obj.position.x-playerMesh.position.x,obj.position.z-playerMesh.position.z);if(d<dist){dist=d;best=obj;}}return best;}
const placedIds=new Set();
function animateBuild(b){if(!b||placedIds.has(b.id))return;placedIds.add(b.id);const g=new THREE.Group();g.position.set(b.x,0,b.z);scene.add(g);const wood=new THREE.MeshStandardMaterial({color:0x79502f});const roof=new THREE.MeshStandardMaterial({color:0x59402e});
 if(b.type==='cabin'||b.type==='shelter'){
  const base=new THREE.Mesh(new THREE.BoxGeometry(4,.18,3.5),wood);base.position.y=.12;g.add(base);
  for(const x of [-1.8,1.8])for(const z of [-1.5,1.5]){const post=new THREE.Mesh(new THREE.BoxGeometry(.18,2.3,.18),wood);post.position.set(x,1.25,z);g.add(post);}
  for(const y of [.5,1.2,1.9]){const wall=new THREE.Mesh(new THREE.BoxGeometry(3.5,.48,.14),wood);wall.position.set(0,y,-1.5);g.add(wall);}
  const r=new THREE.Mesh(new THREE.ConeGeometry(2.8,1.5,4),roof);r.rotation.y=Math.PI/4;r.position.y=2.7;g.add(r);
 }else{for(let i=0;i<3;i++){const log=new THREE.Mesh(new THREE.CylinderGeometry(.13,.13,1.5,8),wood);log.rotation.z=Math.PI/2;log.rotation.y=i*Math.PI/3;log.position.y=.2;g.add(log);}const flame=new THREE.Mesh(new THREE.ConeGeometry(.3,.8,8),new THREE.MeshStandardMaterial({color:0xff8b27,emissive:0xff4500,emissiveIntensity:1}));flame.position.y=.65;g.add(flame);}
 g.scale.set(.01,.01,.01);let start=performance.now();function grow(now){const v=Math.min(1,(now-start)/1700);g.scale.setScalar(v);if(v<1)requestAnimationFrame(grow);}requestAnimationFrame(grow);
}
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
// Detalles faciales, cinturón y guantes para dar más identidad al superviviente.
const browMat=new THREE.MeshStandardMaterial({color:0x38271e});
part(playerMesh,new THREE.BoxGeometry(.09,.018,.025),browMat,[-.085,2.19,.225]);part(playerMesh,new THREE.BoxGeometry(.09,.018,.025),browMat,[.085,2.19,.225]);
part(playerMesh,new THREE.ConeGeometry(.035,.10,8),skinMat,[0,2.09,.245]);
const mouthMat=new THREE.MeshStandardMaterial({color:0x572c27});part(playerMesh,new THREE.BoxGeometry(.075,.012,.012),mouthMat,[0,2.025,.225]);
part(playerMesh,new THREE.BoxGeometry(.72,.12,.42),leatherMat,[0,1.03,0]);
// Hacha visible en la mano derecha; el mango y la hoja giran con el brazo.
const toolGroup=new THREE.Group();toolGroup.position.set(0,-.66,.12);arms[1].add(toolGroup);
const handleMat=new THREE.MeshStandardMaterial({color:0x6a4528,roughness:1});const axeHeadMat=new THREE.MeshStandardMaterial({color:0x9aa5a7,metalness:.72,roughness:.3});
part(toolGroup,new THREE.CylinderGeometry(.035,.045,.72,8),handleMat,[0,-.12,0]);
const axeBlade=part(toolGroup,new THREE.BoxGeometry(.32,.24,.07),axeHeadMat,[.15,.18,0]);axeBlade.rotation.z=-.18;
toolGroup.visible=false;
const spearGroup=new THREE.Group();spearGroup.position.set(0,-.66,.12);arms[1].add(spearGroup);
const spearShaft=part(spearGroup,new THREE.CylinderGeometry(.025,.035,.95,7),handleMat,[0,-.05,0]);
const spearTip=part(spearGroup,new THREE.ConeGeometry(.085,.24,7),axeHeadMat,[0,.53,0]);spearGroup.visible=false;
const woodParticles=[];const particleMat=new THREE.MeshStandardMaterial({color:0xc99a55});
function burstWood(pos){for(let i=0;i<9;i++){const m=new THREE.Mesh(new THREE.BoxGeometry(.045,.045,.12),particleMat);m.position.copy(pos);scene.add(m);woodParticles.push({mesh:m,vel:new THREE.Vector3((Math.random()-.5)*2,1+Math.random()*1.8,(Math.random()-.5)*2),life:.65});}}
async function playHarvestAnimation(obj){
 const node=obj.userData.node;const isTree=node.type==='tree';const isAnimal=node.type==='animal';const isFish=node.type==='fish';const pivot=arms[1];const old=pivot.rotation.x;harvestAnimating=true;toolGroup.visible=!(isAnimal||isFish);spearGroup.visible=(isAnimal||isFish);
 if(isTree){const target=new THREE.Vector3(obj.position.x,1.65,obj.position.z);for(let hit=0;hit<3;hit++){pivot.rotation.x=-1.85;await new Promise(r=>setTimeout(r,170));pivot.rotation.x=.25;obj.rotation.z=.045;obj.rotation.x=(Math.random()-.5)*.035;burstWood(target);await new Promise(r=>setTimeout(r,150));obj.rotation.z=0;obj.rotation.x=0;}}
 else {pivot.rotation.x=-1.65;await new Promise(r=>setTimeout(r,180));pivot.rotation.x=.35;burstWood(new THREE.Vector3(obj.position.x,.7,obj.position.z));await new Promise(r=>setTimeout(r,200));if(isAnimal){obj.rotation.y+=.35;obj.position.y=.08;}if(isFish){obj.position.y=.45;}}
 pivot.rotation.x=old;toolGroup.visible=false;spearGroup.visible=false;harvestAnimating=false;
}
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
 river.material.opacity=.86+Math.sin(t*1.6)*.035;river.position.y=.025+Math.sin(t*1.4)*.012;
 for(let i=0;i<fishSwimmers.length;i++){const f=fishSwimmers[i];if(f.parent){f.position.x+=Math.sin(t*.7+i)*.0008;f.position.y=.22+Math.sin(t*3+i*1.7)*.045;f.rotation.y=Math.sin(t*1.4+i)*.22;}}
 for(const obj of resourceMeshes.values()){if(obj.userData.node?.type==='animal'&&obj.userData.node.active){obj.position.y=Math.sin(t*1.8+obj.position.x)*.018;obj.rotation.y+=Math.sin(t*.45+obj.position.z)*.0007;}}
 if(grassMat.userData.shader)grassMat.userData.shader.uniforms.uWindTime.value=t*1.8;
 for(const tr of animatedTrees){if(tr.userData.node?.active){tr.rotation.z=Math.sin(t*1.2+tr.position.x)*.012;tr.rotation.x=Math.cos(t+tr.position.z)*.009;}}
 for(let i=fallingTrees.length-1;i>=0;i--){const f=fallingTrees[i];f.life-=1/60;f.mesh.rotation.z+=f.dir*.018;f.mesh.rotation.x+=.006;if(f.life<=0){scene.remove(f.mesh);fallingTrees.splice(i,1);}}
 for(let i=woodParticles.length-1;i>=0;i--){const q=woodParticles[i];q.life-=1/60;q.mesh.position.addScaledVector(q.vel,1/60);q.vel.y-=4/60;q.mesh.rotation.x+=.12;if(q.life<=0){scene.remove(q.mesh);woodParticles.splice(i,1);}}
 sun.position.x=Math.sin(t*.025)*25-10;
 const kx=(keys.d||keys.arrowright?1:0)-(keys.a||keys.arrowleft?1:0);
 const ky=(keys.w||keys.arrowup?1:0)-(keys.s||keys.arrowdown?1:0);
 let mx=moveX+kx,my=moveY+ky;const mag=Math.hypot(mx,my);if(mag>1){mx/=mag;my/=mag}
 const moving=mag>.08;const speed=(sprinting||keys.shift?7.2:4.2);
 if(moving){const forwardX=Math.sin(cameraAngle),forwardZ=Math.cos(cameraAngle);const rightX=Math.cos(cameraAngle),rightZ=-Math.sin(cameraAngle);const dx=(rightX*mx+forwardX*my)*speed/60,dz=(rightZ*mx+forwardZ*my)*speed/60;playerMesh.position.x+=dx;playerMesh.position.z+=dz;playerMesh.rotation.y=Math.atan2(dx,dz);}
 const stride=moving?Math.sin(t*(sprinting||keys.shift?13:9))*.62:0;
 legs[0].rotation.x=stride;legs[1].rotation.x=-stride;arms[0].rotation.x=-stride*.65;if(!harvestAnimating)arms[1].rotation.x=stride*.65;
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
// Inicia el formato automático después de que el SDK asíncrono esté disponible.
startMonetagInApp();
</script>
</body></html>"""


@app.get("/")
def index():
    response = make_response(GAME_HTML.replace("__PAYPAL_CLIENT_ID__", PAYPAL_CLIENT_ID))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https: data: blob:; "
        "script-src 'self' 'unsafe-inline' https://telegram.org https://cdn.jsdelivr.net https://libtl.com https://*.libtl.com https://www.paypal.com https://www.paypalobjects.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
        "connect-src 'self' https:; frame-src https://www.paypal.com https://www.sandbox.paypal.com https:;"
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
