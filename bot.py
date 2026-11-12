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

GAME_TITLE = "MI MASCOTA 3D · TAMAGOTCHI"
START_COINS = 100
MAX_INITDATA_AGE_SECONDS = 24 * 60 * 60

PRODUCTS = {
    "vip_fox": {
        "title": "Zorro Aurora VIP",
        "description": "Desbloquea para siempre al Zorro Aurora VIP",
        "price": "2.99",
        "character": "aurora_fox",
    },
    "vip_dragon": {
        "title": "Dragón Rubí VIP",
        "description": "Desbloquea para siempre al Dragón Rubí VIP",
        "price": "4.99",
        "character": "ruby_dragon",
    },
    "vip_axolotl": {
        "title": "Ajolote Cósmico VIP",
        "description": "Desbloquea para siempre al Ajolote Cósmico VIP",
        "price": "3.99",
        "character": "cosmic_axolotl",
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
        "pet_hunger": 85,
        "pet_thirst": 100,
        "pet_happiness": 85,
        "pet_cleanliness": 90,
        "pet_energy": 90,
        "pet_health": 100,
        "pet_sick": False,
        "pet_dead": False,
        "pet_death_at": None,
        "pet_poop": 0,
        "pet_medicine": 2,
        "pet_last_poop": time.time(),
        "pet_game_date": "",
        "pet_games_today": 0,
        "pet_last_game": 0,
        "pet_last_update": time.time(),
        "owned_characters": ["sprout"],
        "active_character": "sprout",
        "pet_born_at": utc_now(),
        "pet_age_days": 0,
        "pet_growth_stage": "Bebé",
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
    player.setdefault("pet_hunger", 85)
    player.setdefault("pet_thirst", 100)
    player.setdefault("pet_happiness", 85)
    player.setdefault("pet_cleanliness", 90)
    player.setdefault("pet_energy", 90)
    player.setdefault("pet_health", 100)
    player.setdefault("pet_sick", False)
    player.setdefault("pet_dead", False)
    player.setdefault("pet_death_at", None)
    player.setdefault("pet_poop", 0)
    player.setdefault("pet_medicine", 2)
    player.setdefault("pet_last_poop", time.time())
    player.setdefault("pet_game_date", "")
    player.setdefault("pet_games_today", 0)
    player.setdefault("pet_last_game", 0)
    player.setdefault("pet_last_update", time.time())
    player.setdefault("owned_characters", ["sprout"])
    if "sprout" not in player["owned_characters"]:
        player["owned_characters"].insert(0, "sprout")
    player.setdefault("active_character", "sprout")
    # La mascota empieza su propia vida cuando se incorpora esta función.
    player.setdefault("pet_born_at", utc_now())
    try:
        born = datetime.fromisoformat(str(player["pet_born_at"]).replace("Z", "+00:00"))
        if born.tzinfo is None:
            born = born.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - born.astimezone(timezone.utc)).days)
    except (TypeError, ValueError):
        player["pet_born_at"] = utc_now()
        age_days = 0
    if age_days < 3:
        growth_stage = "Bebé"
    elif age_days < 7:
        growth_stage = "Pequeño"
    elif age_days < 14:
        growth_stage = "Infantil"
    elif age_days < 30:
        growth_stage = "Adolescente"
    else:
        growth_stage = "Adulto"
    player["pet_age_days"] = age_days
    player["pet_growth_stage"] = growth_stage
    # El tiempo offline se calcula completo (hasta 60 días), no solo las últimas 24 horas.
    elapsed = max(0, min(60 * 86400, time.time() - float(player.get("pet_last_update", time.time()))))
    hours = elapsed / 3600.0
    if not player.get("pet_dead"):
        player["pet_hunger"] = max(0, float(player["pet_hunger"]) - hours * 1.8)
        player["pet_thirst"] = max(0, float(player.get("pet_thirst", 100)) - hours * 2.0)
    player["pet_happiness"] = max(0, float(player["pet_happiness"]) - hours * 1.25)
    player["pet_cleanliness"] = max(0, float(player["pet_cleanliness"]) - hours * 1.0)
    player["pet_energy"] = min(100, float(player["pet_energy"]) + hours * 1.6)
    # La mascota hace sus necesidades de forma natural cada 5 horas, con un máximo de 3.
    now_ts = time.time()
    last_poop = float(player.get("pet_last_poop", now_ts))
    due_piles = max(0, int((now_ts - last_poop) // 18000))
    if due_piles:
        player["pet_poop"] = min(3, int(player.get("pet_poop", 0)) + due_piles)
        player["pet_last_poop"] = last_poop + due_piles * 18000
    # La falta prolongada de higiene o comida puede provocar enfermedad.
    if not player.get("pet_dead") and (float(player["pet_cleanliness"]) < 12 or float(player["pet_hunger"]) < 8 or float(player.get("pet_thirst", 100)) < 8 or int(player.get("pet_poop", 0)) >= 3):
        player["pet_sick"] = True
    if player.get("pet_sick") and not player.get("pet_dead"):
        player["pet_health"] = max(0, float(player.get("pet_health", 100)) - hours * 1.8)
        player["pet_happiness"] = max(0, float(player["pet_happiness"]) - hours * 1.5)
        if player["pet_health"] <= 0:
            player["pet_dead"] = True
            player["pet_death_at"] = utc_now()
    player["pet_last_update"] = now_ts
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
        "pet_hunger", "pet_thirst", "pet_happiness", "pet_cleanliness", "pet_energy", "pet_health", "pet_sick", "pet_dead", "pet_death_at",
        "pet_poop", "pet_medicine", "pet_games_today", "owned_characters", "active_character",
        "pet_born_at", "pet_age_days", "pet_growth_stage",
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


def set_telegram_bot_profile() -> None:
    """Actualiza el nombre y la descripción visibles del bot en Telegram."""
    if not BOT_TOKEN:
        return
    try:
        telegram_api("setMyName", {"name": "Mi Mascota 3D"})
        telegram_api("setMyDescription", {
            "description": "Cuida a tu mascota virtual: dale comida y agua, juega con ella y acompáñala mientras crece."
        })
        telegram_api("setMyShortDescription", {
            "short_description": "Tu mascota virtual 3D. ¡Cuídala cada día!"
        })
    except Exception:
        log.exception("No se pudo actualizar el perfil del bot en Telegram")


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
            "text": "🐾 CUIDAR A MI MASCOTA",
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
        character = product.get("character")
        if character:
            owned = player.setdefault("owned_characters", ["sprout"])
            if character not in owned:
                owned.append(character)
            player["active_character"] = character
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
            send_message(message.get("chat", {}).get("id"), "✅ ¡Compra completada! Tu producto ya está disponible en Mi Mascota 3D.")
        else:
            log.warning("Pago de Telegram no válido o sin charge_id; payload=%s", payload)
        return

    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    text = (message.get("text") or "").strip()
    if not chat_id or not user.get("id"):
        return

    if text.startswith("/start"):
        set_telegram_bot_profile()
        with store_lock:
            data = load_store()
            get_or_create_player(data, user)
            save_store(data)
        send_message(
            chat_id,
            "🐾 ¡Bienvenido a MI MASCOTA 3D!\n\n"
            "Cuida de tu mascota, dale comida y agua, juega con ella y acompáñala mientras crece.\n"
            "¡Tu compañero necesita tus cuidados cada día!",
            mini_app_keyboard(),
        )
    elif text.startswith("/help"):
        send_message(
            chat_id,
            "Comandos disponibles:\n/start - Abrir el juego\n/help - Ayuda\n"
            "Tu progreso se guarda en un archivo JSON local."
        )
    else:
        send_message(chat_id, "Pulsa «🐾 CUIDAR A MI MASCOTA» para continuar.", mini_app_keyboard())


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
                character = product.get("character")
                if character:
                    owned = player.setdefault("owned_characters", ["sprout"])
                    if character not in owned:
                        owned.append(character)
                    player["active_character"] = character
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

            if player.get("pet_dead") and action != "adopt_pet":
                return jsonify(error="Tu mascota ha fallecido. Puedes adoptar una nueva para empezar otra historia."), 410

            # Límite sencillo contra spam de acciones.
            if now - float(player.get("last_action", 0)) < 0.65:
                return jsonify(error="Espera un momento antes de repetir la acción"), 429
            player["last_action"] = now

            if action == "select_character":
                character = str(body.get("character", ""))
                if character not in player.setdefault("owned_characters", ["sprout"]):
                    return jsonify(error="Todavía no has desbloqueado ese personaje VIP"), 403
                player["active_character"] = character
                message = "¡Tu nueva mascota ya está lista!"
            elif action == "feed_pet":
                food_type = str(body.get("food_type", "kibble"))
                food_values = {"kibble": (28, 2, 0, "pienso"), "fruit": (20, 8, 0, "fruta"), "fish": (35, 5, 2, "pescado")}
                if food_type not in food_values:
                    return jsonify(error="Tipo de comida no válido"), 400
                if player["food"] < 1:
                    return jsonify(error="No tienes pienso. Vuelve mañana o consigue una recompensa."), 400
                hunger_gain, happy_gain, health_gain, food_name = food_values[food_type]
                player["food"] -= 1
                player["pet_hunger"] = min(100, float(player["pet_hunger"]) + hunger_gain)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + happy_gain)
                player["pet_health"] = min(100, float(player.get("pet_health", 100)) + health_gain)
                message = f"¡Ñam! Ha comido {food_name}."
            elif action == "give_water":
                if int(player.get("water", 0)) < 1:
                    return jsonify(error="No tienes agua. Recoge el regalo diario para conseguir más."), 400
                player["water"] -= 1
                player["pet_thirst"] = min(100, float(player.get("pet_thirst", 100)) + 42)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 3)
                message = "¡Glup, glup! Tu mascota ha bebido agua fresca."
            elif action == "adopt_pet":
                player.update({"pet_hunger": 85, "pet_thirst": 100, "pet_happiness": 85, "pet_cleanliness": 90, "pet_energy": 90, "pet_health": 100, "pet_sick": False, "pet_dead": False, "pet_death_at": None, "pet_poop": 0, "pet_medicine": max(1, int(player.get("pet_medicine", 0))), "pet_last_poop": time.time(), "pet_last_update": time.time(), "pet_born_at": utc_now(), "pet_age_days": 0, "pet_growth_stage": "Bebé"})
                player["active_character"] = "sprout"
                message = "Has adoptado una nueva mascota bebé. Cuídala cada día."
            elif action == "play_pet":
                if player["pet_energy"] < 8:
                    return jsonify(error="Tu mascota está cansada. Déjala dormir un poco."), 400
                player["pet_energy"] = max(0, float(player["pet_energy"]) - 8)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 22)
                player["pet_hunger"] = max(0, float(player["pet_hunger"]) - 3)
                apply_xp(player, 8)
                message = "¡Qué divertido! Tu mascota está más contenta."
            elif action == "clean_pet":
                player["pet_cleanliness"] = min(100, float(player["pet_cleanliness"]) + 38)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 5)
                message = "Baño terminado. ¡Tu mascota está reluciente!"
            elif action == "clean_poop":
                if int(player.get("pet_poop", 0)) < 1:
                    return jsonify(error="No hay cacas que limpiar. ¡El jardín está limpio!"), 400
                player["pet_poop"] = max(0, int(player.get("pet_poop", 0)) - 1)
                player["pet_cleanliness"] = min(100, float(player["pet_cleanliness"]) + 8)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 2)
                message = "¡Has recogido sus necesidades! El jardín vuelve a estar limpio."
            elif action == "give_medicine":
                if not player.get("pet_sick"):
                    return jsonify(error="Tu mascota está sana. No necesita medicinas."), 400
                if int(player.get("pet_medicine", 0)) < 1:
                    return jsonify(error="No tienes medicina. Consigue una en el regalo diario."), 400
                player["pet_medicine"] -= 1
                player["pet_sick"] = False
                player["pet_health"] = min(100, float(player.get("pet_health", 100)) + 45)
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 8)
                message = "La medicina ha hecho efecto. ¡Cuida su higiene y alimentación para que no recaiga!"
            elif action == "mini_game":
                game_id = str(body.get("game", ""))
                if game_id not in ("catch", "memory", "dance"):
                    return jsonify(error="Juego no válido"), 400
                today = datetime.now(timezone.utc).date().isoformat()
                if player.get("pet_game_date") != today:
                    player["pet_game_date"] = today
                    player["pet_games_today"] = 0
                if int(player.get("pet_games_today", 0)) >= 10:
                    return jsonify(error="Has jugado los 10 juegos de hoy. ¡Mañana habrá más!"), 400
                if now - float(player.get("pet_last_game", 0)) < 12:
                    return jsonify(error="Espera unos segundos antes de volver a jugar."), 429
                player["pet_games_today"] = int(player.get("pet_games_today", 0)) + 1
                player["pet_last_game"] = now
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 12)
                player["pet_energy"] = max(0, float(player["pet_energy"]) - 4)
                apply_xp(player, 5)
                message = {"catch":"¡Buen trabajo atrapando estrellas!","memory":"¡Qué buena memoria! Tu mascota está feliz.","dance":"¡Menudo baile! Os lo habéis pasado genial."}[game_id]
            elif action == "sleep_pet":
                player["pet_energy"] = min(100, float(player["pet_energy"]) + 32)
                player["pet_hunger"] = max(0, float(player["pet_hunger"]) - 2)
                message = "Tu mascota ha descansado un rato."
            elif action == "daily":
                today = datetime.now(timezone.utc).date().isoformat()
                if player.get("last_daily") == today:
                    return jsonify(error="Ya has recogido la recompensa diaria"), 400
                player["last_daily"] = today
                player["food"] += 2
                player["pet_medicine"] = min(5, int(player.get("pet_medicine", 0)) + 1)
                player["coins"] += 15
                player["pet_happiness"] = min(100, float(player["pet_happiness"]) + 8)
                message = "Regalo diario: pienso, monedas y cariño para tu mascota."
            elif action == "harvest_node":
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
<title>Mi Mascota 3D - Tamagotchi</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script async src="https://libtl.com/sdk.js" data-zone="12235771" data-sdk="show_12235771"></script>
<script>window.ISLA_PAYPAL_CLIENT_ID="__PAYPAL_CLIENT_ID__";</script>
<style>
:root{color-scheme:dark;--panel:rgba(19,22,43,.78);--line:rgba(255,255,255,.18);--green:#a8f4d2;--pink:#ff9ecb}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#182448,#452b63 58%,#173f54);color:#fff;font-family:system-ui,Arial;overflow:hidden}
#scene{position:fixed;inset:0;z-index:0}canvas{display:block}
.hud{position:fixed;z-index:2;left:10px;right:10px;top:10px;display:flex;gap:8px;align-items:flex-start;justify-content:space-between;pointer-events:none}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:9px 11px;backdrop-filter:blur(10px);box-shadow:0 6px 25px #0005}
.brand{font-weight:900;letter-spacing:1px;font-size:13px}.sub{font-size:10px;color:#b8c8bf}
.petStats{min-width:190px;display:grid;gap:7px;font-size:11px}.meter{height:7px;background:#ffffff25;border-radius:99px;overflow:hidden;margin-top:3px}.meter i{display:block;height:100%;width:80%;border-radius:99px;background:linear-gradient(90deg,#8ef0c1,#ffe18a);transition:width .35s}.petName{font-size:17px;font-weight:900}.petMood{font-size:11px;color:#ffe1f0}.petActions{position:fixed;z-index:5;left:50%;transform:translateX(-50%);bottom:max(14px,env(safe-area-inset-bottom));display:flex;gap:8px;flex-wrap:wrap;justify-content:center;width:min(94vw,620px)}.petActions button{min-width:88px;background:linear-gradient(145deg,#59458b,#293f73);border:1px solid #ffffff35;box-shadow:0 7px 20px #070d2b70}.petActions button.primary{background:linear-gradient(145deg,#e66da9,#a747a5)}.petTop{display:flex;gap:8px;align-items:center}.petBadge{border-radius:99px;background:#ffffff20;padding:5px 9px;font-size:10px}

button{border:1px solid var(--line);background:rgba(19,38,32,.94);color:#fff;border-radius:12px;padding:11px 7px;font-weight:700;font-size:12px;min-height:43px}
button:active{transform:scale(.97);background:#315b42}button.primary{background:#267747;border-color:#4b9b69}
#toast{position:fixed;z-index:8;left:50%;top:75px;transform:translateX(-50%);background:#172c24;border:1px solid #ffffff30;border-radius:12px;padding:10px 14px;display:none;max-width:90%;font-size:13px;text-align:center}
#shop,#games{position:fixed;z-index:6;inset:0;background:#000b;display:none;align-items:flex-end;justify-content:center}
.gameSheet{max-width:560px;margin:auto;border-radius:22px!important}
.gameChoices{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.gameChoices button{min-height:74px;background:linear-gradient(145deg,#55418b,#243f72)}.gameChoices small{display:block;color:#d8d4ef;font-size:10px;margin-top:5px}
#miniGameArea{min-height:170px;margin-top:12px;padding:12px;border-radius:16px;background:#0b171dcc;text-align:center}
.gameTarget{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:50%;width:58px;height:58px;margin:7px;background:linear-gradient(145deg,#ffe58b,#ff8dc4);font-size:27px;box-shadow:0 5px 18px #ffb8d866}
.memoryGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;max-width:330px;margin:10px auto}.memoryCard{height:58px;font-size:22px;background:#3d3472}
.sickAlert{display:none;position:fixed;z-index:4;top:125px;left:50%;transform:translateX(-50%);background:#6b2938eF;border:1px solid #ff9cac;border-radius:12px;padding:9px 13px;font-size:12px;max-width:92vw;text-align:center}
.sheet{width:100%;max-height:78vh;overflow:auto;background:#111f1b;border:1px solid #ffffff22;border-radius:20px 20px 0 0;padding:16px}
.item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid #ffffff18}
.item small{display:block;color:#b9c9c0;margin-top:3px}.item button{min-width:95px}
#walletPanel input{width:100%;padding:12px;border-radius:9px;border:1px solid #ffffff35;background:#14251f;color:#fff;margin:6px 0}#walletPanel .walletBig{font-size:24px;font-weight:900;color:#9be4ad}#walletPanel .walletNote{font-size:11px;color:#b9c9c0;line-height:1.45}
#paypalPanel{display:none;margin-top:14px;padding:13px;border:1px solid #ffffff25;border-radius:14px;background:#0b1713}#paypal-button-container{min-height:48px}#paypalStatus{font-size:12px;color:#c9d8cf;margin:8px 0}
#hint{position:fixed;z-index:2;left:50%;bottom:104px;transform:translateX(-50%);background:#07120f99;padding:7px 12px;border-radius:20px;font-size:11px;color:#e5eee8;white-space:nowrap;pointer-events:none;max-width:90vw;overflow:hidden;text-overflow:ellipsis}
@media(max-width:600px){.hud{left:7px;right:7px;top:7px;gap:6px}.panel{padding:8px}.petStats{min-width:145px;font-size:10px}.petName{font-size:15px}.petActions{gap:5px;bottom:max(8px,env(safe-area-inset-bottom));width:98vw}.petActions button{min-width:76px;min-height:38px;padding:8px 5px;font-size:10px}#hint{bottom:104px;font-size:10px}.gameChoices{grid-template-columns:1fr}.gameChoices button{min-height:52px}.gameSheet{max-height:88vh;padding:13px}.sickAlert{top:118px;font-size:11px}}
@media(min-width:700px){#hint{bottom:94px}.hud{max-width:900px;left:50%;right:auto;transform:translateX(-50%);width:calc(100% - 24px)}.petActions{bottom:22px}}
@media(orientation:landscape) and (max-height:500px){.petActions{width:70vw;left:auto;right:10px;transform:none;bottom:8px}.petStats{min-width:160px}#hint{bottom:70px}}
</style>
</head>
<body>
<div id="scene"></div>
<div class="hud">
 <div class="panel">
  <div class="brand">🐾 MI MASCOTA 3D · TAMAGOTCHI</div>
  <div class="petName" id="petName">Sprout</div>
  <div class="petBadge" id="petAge">👶 Bebé · Día 1</div>
  <div class="petMood" id="petMood">Tu nueva amistad te espera 💖</div>
  <div class="sub">💊 Medicina: <b id="medicineCountTop">2</b> · 💩 Necesidades: <b id="poopCountTop">0</b></div>
  <div class="sub" id="level">Nivel 1</div><div class="sub" id="xp">0 / 100 XP</div><div class="sub">🍓 Pienso disponible: <b id="foodCount">2</b></div>
 </div>
 <div class="panel petStats">
  <div>🍓 Hambre <b id="petHungerText">85%</b><div class="meter"><i id="petHungerBar"></i></div></div>
  <div>💧 Sed <b id="petThirstText">100%</b><div class="meter"><i id="petThirstBar"></i></div></div>
  <div>💗 Felicidad <b id="petHappyText">85%</b><div class="meter"><i id="petHappyBar"></i></div></div>
  <div>🫧 Limpieza <b id="petCleanText">90%</b><div class="meter"><i id="petCleanBar"></i></div></div>
  <div>⚡ Energía <b id="petEnergyText">90%</b><div class="meter"><i id="petEnergyBar"></i></div></div>
 </div>
</div>
<div id="hint">Cuida de tu mascota, juega con ella y descubre sus reacciones ✨</div>
<div id="toast"></div>
<div class="petActions">
 <button class="primary" onclick="openFeedMenu()">🍓 Dar de comer</button>
 <button onclick="petAction('give_water')">💧 Dar agua <b id="waterCount">2</b></button>
 <button onclick="petAction('play_pet')">🎾 Jugar</button>
 <button onclick="petAction('clean_pet')">🫧 Bañar</button>
 <button onclick="petAction('clean_poop')">🧹 Recoger caca <b id="poopCount">0</b></button>
 <button onclick="petAction('give_medicine')">💊 Medicina <b id="medicineCount">2</b></button>
 <button onclick="openGames()">🎮 Juegos</button>
 <button onclick="petAction('sleep_pet')">🌙 Dormir</button>
 <button onclick="petAction('daily')">🎁 Regalo</button>
 <button id="rewardedAdButton" onclick="showRewardedAd()">🎬 Anuncio</button>
 <button onclick="openShop()">👑 Personajes VIP</button>
</div>
<div id="feedMenu" class="sheet" style="display:none;position:fixed;z-index:12;left:50%;top:50%;transform:translate(-50%,-50%);width:min(92vw,420px)"><h3>🍽️ ¿Qué le damos de comer?</h3><p class="sub">Cada comida tiene un efecto distinto.</p><div class="gameChoices"><button onclick="feedPet('kibble')">🥣 Pienso<small>+28 hambre</small></button><button onclick="feedPet('fruit')">🍎 Fruta<small>+20 hambre · +8 felicidad</small></button><button onclick="feedPet('fish')">🐟 Pescado<small>+35 hambre · +2 salud</small></button></div><button onclick="closeFeedMenu()">Cancelar</button></div>
<div id="deadOverlay" style="display:none;position:fixed;z-index:20;inset:0;background:#080b18ed;color:white;align-items:center;justify-content:center;text-align:center;padding:24px"><div class="sheet" style="max-width:440px"><div style="font-size:64px">🕯️</div><h2>Tu mascota ha fallecido</h2><p>Ha pasado demasiado tiempo sin cuidados. Esta historia ha terminado, pero puedes darle un nuevo hogar a otra mascota bebé.</p><button class="primary" onclick="adoptNewPet()">🐣 Adoptar otra mascota</button></div></div>
<div id="games"><div class="sheet gameSheet">
 <div style="display:flex;justify-content:space-between;align-items:center"><div><h3>🎮 ¡Vamos a jugar!</h3><p class="sub">Juega con tu mascota para subir su felicidad. Máximo 10 partidas al día.</p></div><button onclick="closeGames()">Cerrar ✕</button></div>
 <div class="gameChoices">
  <button onclick="startMiniGame('catch')">⭐ Atrapa estrellas<small>Consigue 5 estrellas</small></button>
  <button onclick="startMiniGame('memory')">🧠 Memoria<small>Encuentra 4 parejas</small></button>
  <button onclick="startMiniGame('dance')">🎵 Baile<small>Sigue el ritmo</small></button>
 </div>
 <div id="miniGameArea"><p class="sub">Elige un juego para empezar.</p></div>
 </div></div>
<div id="sickAlert" class="sickAlert">🤒 Tu mascota está enferma. Límpiala, aliméntala y dale medicina.</div>
<div id="shop"><div class="sheet">
 <div style="display:flex;justify-content:space-between;align-items:center"><div><h3>👑 Personajes VIP</h3><p class="sub">Desbloquea criaturas especiales para siempre. La mascota básica es gratuita.</p></div><button onclick="closeShop()">Cerrar ✕</button></div>
 <div class="item"><div><b>🦊 Zorro Aurora VIP</b><small>Pelaje brillante y detalles mágicos · 2,99 €</small></div><button id="buyFox" onclick="buy('vip_fox')">Desbloquear</button></div>
 <div class="item"><div><b>🐉 Dragón Rubí VIP</b><small>Alas, cuernos y escamas rubí · 4,99 €</small></div><button id="buyDragon" onclick="buy('vip_dragon')">Desbloquear</button></div>
 <div class="item"><div><b>🩵 Ajolote Cósmico VIP</b><small>Branquias luminosas y cola acuática · 3,99 €</small></div><button id="buyAxolotl" onclick="buy('vip_axolotl')">Desbloquear</button></div>
 <p class="sub">Los personajes VIP son cosméticos: no dan ventajas ni hacen falta para cuidar y jugar. Pago seguro mediante PayPal.</p>
 <div id="paypalPanel"><div style="display:flex;justify-content:space-between;align-items:center"><b>🔒 Pago seguro con PayPal</b><button onclick="closePaypal()">Cerrar</button></div><p id="paypalStatus">Elige PayPal para completar tu compra.</p><div id="paypal-button-container"></div></div>
 </div></div>

<script type="module">
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js';

const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
let player = null;
let petGrowthScale = 0.58;
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
 const petFields=[['pet_hunger','petHunger'],['pet_thirst','petThirst'],['pet_happiness','petHappy'],['pet_cleanliness','petClean'],['pet_energy','petEnergy']];
 for(const [field,id] of petFields){const v=Math.round(Number(p[field]??0));document.getElementById(id+'Text').textContent=v+'%';document.getElementById(id+'Bar').style.width=v+'%';}
 const mood=p.pet_sick?'No se encuentra bien. Necesita cuidados 🤒':p.pet_happiness>75?'¡Está encantada de verte! 💖':p.pet_happiness>40?'Quiere jugar contigo 🥰':'Necesita un poco de cariño 🥺';
 document.getElementById('petMood').textContent=mood;
 const sick=document.getElementById('sickAlert');if(sick)sick.style.display=p.pet_sick?'block':'none';
 const dead=document.getElementById('deadOverlay');if(dead)dead.style.display=p.pet_dead?'flex':'none';
 const poop=Number(p.pet_poop||0),medicine=Number(p.pet_medicine||0);
 for(const id of ['poopCount','poopCountTop']){const el=document.getElementById(id);if(el)el.textContent=poop;}
 for(const id of ['medicineCount','medicineCountTop']){const el=document.getElementById(id);if(el)el.textContent=medicine;}
 if(typeof syncPoopVisuals==='function')syncPoopVisuals(poop);
 document.getElementById('petName').textContent=({sprout:'Sprout',aurora_fox:'Zorro Aurora',ruby_dragon:'Dragón Rubí',cosmic_axolotl:'Ajolote Cósmico'})[p.active_character||'sprout']||'Sprout';
 const ageDays=Math.max(0,Number(p.pet_age_days||0));
 const stage=p.pet_growth_stage||'Bebé';
 document.getElementById('petAge').textContent=`${({Bebé:'👶',Pequeño:'🐣',Infantil:'🧸',Adolescente:'✨',Adulto:'🌟'})[stage]||'👶'} ${stage} · Día ${ageDays+1}`;
 petGrowthScale=ageDays<3?.58:ageDays<7?.68:ageDays<14?.79:ageDays<30?.9:1.0;
 applyPetCharacter(p.active_character||'sprout');
 const owned=p.owned_characters||['sprout'];
 [['aurora_fox','buyFox','vip_fox'],['ruby_dragon','buyDragon','vip_dragon'],['cosmic_axolotl','buyAxolotl','vip_axolotl']].forEach(([id,buttonId,product])=>{const b=document.getElementById(buttonId);if(!b)return;if(owned.includes(id)){b.textContent=p.active_character===id?'En uso':'Usar';b.onclick=()=>selectCharacter(id);}else{b.textContent='Desbloquear';b.onclick=()=>buy(product);}});
 document.getElementById('foodCount').textContent=Number(p.food||0);
 const waterCount=document.getElementById('waterCount');if(waterCount)waterCount.textContent=Number(p.water||0);
 const rewardBtn=document.getElementById('rewardedAdButton'); if(rewardBtn){const capped=Number(p.rewarded_ads_count||0)>=10;rewardBtn.disabled=capped;rewardBtn.textContent=capped?'Límite diario alcanzado':'🎬 Anuncio + pienso';}
 document.getElementById('level').textContent=`Nivel ${p.level}`;
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
window.act=async(action,extra={})=>{try{const j=await api('/api/action',{action,...extra});renderPlayer(j.player);toast(j.message);return j}catch(e){toast(e.message);return null}};
window.petAction=async(action)=>{const result=await window.act(action);if(result){petReact(action);}};
window.openFeedMenu=()=>{if(player?.pet_dead)return;document.getElementById('feedMenu').style.display='block';};
window.closeFeedMenu=()=>document.getElementById('feedMenu').style.display='none';
window.feedPet=async(food_type)=>{const result=await window.act('feed_pet',{food_type});if(result){closeFeedMenu();petReact('feed_'+food_type);}};
window.adoptNewPet=async()=>{const result=await window.act('adopt_pet');if(result){renderPlayer(result.player);document.getElementById('deadOverlay').style.display='none';petReact('adopt_pet');}};
window.selectCharacter=async(character)=>{const result=await window.act('select_character',{character});if(result)applyPetCharacter(character);};

window.openShop=()=>{document.getElementById('shop').style.display='flex';if(player)renderPlayer(player)};
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
     renderPlayer(result.player);
     status.textContent='¡Pago confirmado! Tu compra ya está en tu inventario.';
     toast('✅ Compra recibida. ¡Gracias por apoyar Mi Mascota 3D!');
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
// Minijuegos táctiles: cada partida tiene una interacción real y anima a la mascota.
let miniGameTimer=null;
window.openGames=()=>{if(player?.pet_dead)return;document.getElementById('games').style.display='flex';};
window.closeGames=()=>{document.getElementById('games').style.display='none';if(miniGameTimer)clearInterval(miniGameTimer);};
window.startMiniGame=async(kind)=>{
 if(!['catch','memory','dance'].includes(kind))return;
 petReact('mini_game');
 const area=document.getElementById('miniGameArea');area.innerHTML='<p class="sub">Preparando el juego…</p>';
 if(kind==='catch'){
  let score=0;area.innerHTML='<p>⭐ Toca 5 estrellas antes de que desaparezcan.</p><div id="catchScore">0 / 5</div><div id="catchTargets"></div>';
  const spawn=()=>{const wrap=document.getElementById('catchTargets');if(!wrap)return;wrap.innerHTML='';const b=document.createElement('button');b.className='gameTarget';b.textContent='⭐';b.onclick=()=>{score++;document.getElementById('catchScore').textContent=score+' / 5';if(score>=5)finishMiniGame(kind);else spawn();};wrap.appendChild(b);};spawn();
 }else if(kind==='memory'){
  const vals=['🍓','⭐','💎','🌸'];const cards=[...vals,...vals].sort(()=>Math.random()-.5);let opened=[],matched=0;area.innerHTML='<p>Encuentra las cuatro parejas.</p><div id="memoryGrid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px"></div>';
  cards.forEach((v,i)=>{const b=document.createElement('button');b.className='gameTarget';b.textContent='❔';b.onclick=()=>{if(b.dataset.done||opened.length>=2||b.textContent!=='❔')return;b.textContent=v;opened.push({b,v});if(opened.length===2){if(opened[0].v===opened[1].v){opened.forEach(x=>x.b.dataset.done='1');matched++;opened=[];if(matched===4)finishMiniGame(kind);}else{const pair=opened;setTimeout(()=>{pair.forEach(x=>x.b.textContent='❔');opened=[];},650);}}};document.getElementById('memoryGrid').appendChild(b);});
 }else{
  let taps=0;const sequence=['⬅️','➡️','⬆️','⬇️','⬅️'];area.innerHTML='<p>Sigue la secuencia de baile tocando las flechas:</p><h2 id="danceCue">⬅️</h2><button class="gameTarget" onclick="danceTap()">💃</button><p id="danceScore">0 / 5</p>';window.danceTap=()=>{if(sequence[taps]){taps++;document.getElementById('danceScore').textContent=taps+' / 5';document.getElementById('danceCue').textContent=sequence[taps]||'🎉';if(taps===5)finishMiniGame(kind);}};
 }
};
async function finishMiniGame(kind){const result=await window.act('mini_game',{game:kind});if(result){petReact('mini_game');document.getElementById('miniGameArea').innerHTML='<h3>🎉 ¡Lo has conseguido!</h3><p>'+result.message+'</p><button onclick="startMiniGame(''+kind+'')">Volver a jugar</button>';}}

// Monetag SDK: esperamos a que el script asíncrono esté realmente disponible.
// La recompensa económica se acredita exclusivamente mediante postback S2S.
// Espaciado adicional entre anuncios iniciados por el jugador: 15 minutos.
const AD_COOLDOWN_MS=15*60*1000;
function adCooldownRemaining(){
 try{return Math.max(0,AD_COOLDOWN_MS-(Date.now()-Number(localStorage.getItem('mascota_last_ad_at')||0)));}
 catch(_){return 0;}
}
function markAdShown(){try{localStorage.setItem('mascota_last_ad_at',String(Date.now()));}catch(_){}}
function canShowManualAd(){
 const remaining=adCooldownRemaining();
 if(remaining>0){const mins=Math.ceil(remaining/60000);toast('Para que no salgan tan seguidos, espera '+mins+' min para ver otro anuncio.');return false;}
 return true;
}
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
 if(!canShowManualAd())return;
 if(player && Number(player.rewarded_ads_count||0)>=10){toast('Has alcanzado los 10 anuncios recompensados de hoy.');return;}
 try{
  const showAd=await waitForMonetag();
  await Promise.resolve(showAd());
  markAdShown();
  toast('Anuncio terminado. La recompensa llegará cuando Monetag confirme el postback.');
  setTimeout(()=>load(),4000);
 }catch(e){console.warn('Monetag rewarded:',e);toast('No se pudo mostrar el anuncio: '+(e?.message||'error'));}
};
window.showPopupAd=async()=>{
 if(!canShowManualAd())return;
 if(player && Number(player.rewarded_ads_count||0)>=10){toast('Has alcanzado los 10 anuncios recompensados de hoy.');return;}
 try{
  const showAd=await waitForMonetag();
  await Promise.resolve(showAd('pop'));
  markAdShown();
  toast('Anuncio solicitado. La recompensa depende de la confirmación de Monetag.');
  setTimeout(()=>load(),4000);
 }catch(e){console.warn('Monetag popup:',e);toast('No se pudo mostrar el anuncio: '+(e?.message||'error'));}
};
async function startMonetagInApp(){
 try{
  const showAd=await waitForMonetag();
  // Frecuencia reducida: máximo 1 anuncio por ciclo, capping de 1 hora,
  // al menos 10 minutos entre impresiones, 5s de espera inicial y sin repetir por página.
  await Promise.resolve(showAd({type:'inApp',inAppSettings:{frequency:1,capping:1,interval:600,timeout:5,everyPage:false}}));
  console.info('Monetag In-App Interstitial iniciado.');
 }catch(e){console.warn('Monetag In-App Interstitial:',e);}
}
async function captureIfReturned(){ /* PayPal captura el pedido desde onApprove; la partida permanece abierta. */ }

// Escena 3D mejorada: vegetación animada, modelos orgánicos y acciones físicas.
const host=document.getElementById('scene');
const scene=new THREE.Scene(); scene.background=new THREE.Color(0x9bb8df); scene.fog=new THREE.Fog(0x9bb8df,22,70);
const camera=new THREE.PerspectiveCamera(65,innerWidth/innerHeight,.1,300);
camera.position.set(0,8,16);
const renderer=new THREE.WebGLRenderer({antialias:true,alpha:false,powerPreference:'high-performance'});
renderer.setPixelRatio(Math.min(devicePixelRatio,2)); renderer.setSize(innerWidth,innerHeight); renderer.shadowMap.enabled=true; renderer.shadowMap.type=THREE.PCFSoftShadowMap;
renderer.outputColorSpace=THREE.SRGBColorSpace; renderer.toneMapping=THREE.ACESFilmicToneMapping; renderer.toneMappingExposure=1.12;
host.appendChild(renderer.domElement);
scene.add(new THREE.HemisphereLight(0xd9f3ff,0x44513a,2.1));
const sun=new THREE.DirectionalLight(0xffe4b5,3.0); sun.position.set(-20,35,15); sun.castShadow=true; sun.shadow.mapSize.set(2048,2048); sun.shadow.camera.left=-55; sun.shadow.camera.right=55; sun.shadow.camera.top=55; sun.shadow.camera.bottom=-55; scene.add(sun);
const hemi=scene.children.find(o=>o.isHemisphereLight);
const moon=new THREE.Mesh(new THREE.SphereGeometry(2.1,20,16),new THREE.MeshBasicMaterial({color:0xe7edff}));
moon.position.set(0,34,-48);scene.add(moon);
const starPositions=new Float32Array(420*3);
for(let i=0;i<420;i++){starPositions[i*3]=(Math.random()-.5)*190;starPositions[i*3+1]=25+Math.random()*65;starPositions[i*3+2]=(Math.random()-.5)*190;}
const starGeo=new THREE.BufferGeometry();starGeo.setAttribute('position',new THREE.BufferAttribute(starPositions,3));
const stars=new THREE.Points(starGeo,new THREE.PointsMaterial({color:0xffffff,size:.48,transparent:true,opacity:0,depthWrite:false}));scene.add(stars);
// Nubes volumétricas sencillas y partículas de lluvia reutilizables.
const cloudGroup=new THREE.Group();const cloudMat=new THREE.MeshStandardMaterial({color:0xd6dfdf,roughness:1,transparent:true,opacity:.78});
for(let i=0;i<13;i++){const c=new THREE.Group();for(let j=0;j<5;j++){const puff=new THREE.Mesh(new THREE.SphereGeometry(1.5+Math.random()*1.5,10,8),cloudMat);puff.position.set((j-2)*1.7,Math.random()*.8,Math.random()*1.4);puff.scale.y=.48;c.add(puff);}c.position.set((Math.random()-.5)*100,17+Math.random()*8,(Math.random()-.5)*90);c.userData.speed=.35+Math.random()*.55;cloudGroup.add(c);}scene.add(cloudGroup);
const rainCount=900,rainPositions=new Float32Array(rainCount*3),rainSpeeds=new Float32Array(rainCount);
for(let i=0;i<rainCount;i++){rainPositions[i*3]=(Math.random()-.5)*75;rainPositions[i*3+1]=Math.random()*28;rainPositions[i*3+2]=(Math.random()-.5)*75;rainSpeeds[i]=12+Math.random()*9;}
const rainGeo=new THREE.BufferGeometry();rainGeo.setAttribute('position',new THREE.BufferAttribute(rainPositions,3));
const rain=new THREE.Points(rainGeo,new THREE.PointsMaterial({color:0xb9d9e8,size:.095,transparent:true,opacity:0,depthWrite:false}));scene.add(rain);
let weather='clear',nextWeatherAt=performance.now()+65000,lightningUntil=0;
const weatherNames={clear:'Despejado',cloudy:'Nublado',rain:'Lluvia',storm:'Tormenta',fog:'Niebla'};
function chooseWeather(now){const roll=Math.random();weather=roll<.36?'clear':roll<.57?'cloudy':roll<.79?'rain':roll<.9?'fog':'storm';nextWeatherAt=now+(55000+Math.random()*65000);const label=document.getElementById('worldWeather');if(label)label.textContent=(dayFactor>.2?'☀️ Día':'🌙 Noche')+' · '+weatherNames[weather];}
let dayFactor=1;

const ground=new THREE.Mesh(new THREE.PlaneGeometry(220,220,90,90),new THREE.MeshStandardMaterial({color:0x648b72,roughness:1}));
// Ondulación suave del terreno para evitar una superficie completamente plana.
const gp=ground.geometry.attributes.position;for(let i=0;i<gp.count;i++){const x=gp.getX(i),z=gp.getY(i);gp.setZ(i,Math.sin(x*.17)*.10+Math.cos(z*.21)*.08+Math.sin((x+z)*.08)*.06);}ground.geometry.computeVertexNormals();ground.rotation.x=-Math.PI/2;ground.receiveShadow=true;scene.add(ground);
// Hierba en instancias: muchas briznas con movimiento de viento en el shader.
const grassMat=new THREE.MeshStandardMaterial({color:0x9acb91,roughness:1,side:THREE.DoubleSide});
grassMat.onBeforeCompile=(shader)=>{shader.vertexShader=shader.vertexShader.replace('#include <common>','#include <common>\nuniform float uWindTime;');shader.vertexShader=shader.vertexShader.replace('#include <begin_vertex>','#include <begin_vertex>\ntransformed.x += sin(uWindTime + instanceMatrix[3].x*0.17 + instanceMatrix[3].z*0.13) * position.y * 0.10;');shader.uniforms.uWindTime={value:0};grassMat.userData.shader=shader;};
const bladeGeo=new THREE.ConeGeometry(.045,.42,3);bladeGeo.translate(0,.21,0);const grass=new THREE.InstancedMesh(bladeGeo,grassMat,1800);const dummy=new THREE.Object3D();let gi=0;for(let i=0;i<1800;i++){const x=(Math.random()-.5)*42,z=(Math.random()-.5)*42;if(Math.hypot(x/1.25,z/.85)>20)continue;dummy.position.set(x,.05,z);dummy.rotation.y=Math.random()*Math.PI;dummy.scale.setScalar(.55+Math.random()*.8);dummy.updateMatrix();grass.setMatrixAt(gi++,dummy.matrix);}grass.count=gi;grass.instanceMatrix.needsUpdate=true;scene.add(grass);
// Jardín de mascota: pequeñas flores, setas y piedras decorativas alrededor del hogar.
const garden=new THREE.Group();const flowerColors=[0xff9ecb,0xffdf87,0xb8a0ff,0xffffff];
for(let i=0;i<44;i++){const a=Math.random()*Math.PI*2,r=3.8+Math.random()*7,x=Math.cos(a)*r,z=Math.sin(a)*r;const stem=new THREE.Mesh(new THREE.CylinderGeometry(.018,.025,.24,5),new THREE.MeshStandardMaterial({color:0x4b9a63}));stem.position.set(x,.12,z);garden.add(stem);const bloom=new THREE.Mesh(new THREE.SphereGeometry(.105,10,8),new THREE.MeshStandardMaterial({color:flowerColors[i%flowerColors.length],emissive:flowerColors[i%flowerColors.length],emissiveIntensity:.08}));bloom.position.set(x,.29,z);garden.add(bloom);}
scene.add(garden);
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
// Árboles ornamentales del jardín, colocados alrededor del espacio de juego.
for(let i=0;i<13;i++){
 const a=i/13*Math.PI*2,rad=12.5+(i%3)*1.6,x=Math.cos(a)*rad,z=Math.sin(a)*rad;
 const decor=new THREE.Group();decor.position.set(x,0,z);const h=2.8+(i%4)*.32;
 const trunk=new THREE.Mesh(new THREE.CylinderGeometry(.17,.29,h,9),trunkMat);trunk.position.y=h/2;trunk.castShadow=true;decor.add(trunk);
 for(let j=0;j<5;j++){const crown=new THREE.Mesh(new THREE.IcosahedronGeometry(.9+(j%2)*.18,1),j%3===0?leafLightMat:leafMat);crown.position.set(Math.sin(j*2.3)*.48,h*.72+(j%3)*.42,Math.cos(j*2.3)*.42);crown.scale.set(1.12,.95,1);crown.castShadow=true;decor.add(crown);}
 scene.add(decor);
}
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
// Mascota 3D original: cuerpo orgánico, ojos con brillo, hocico, orejas, patas y cola articulada.
const playerMesh=new THREE.Group();
const furMat=new THREE.MeshStandardMaterial({color:0x83d9b5,roughness:.62});
const bellyMat=new THREE.MeshStandardMaterial({color:0xffe4c9,roughness:.78});
const innerMat=new THREE.MeshStandardMaterial({color:0xffa9c8,roughness:.65});
const eyeWhite=new THREE.MeshStandardMaterial({color:0xffffff,roughness:.22});
const pupilMat=new THREE.MeshPhysicalMaterial({color:0x17162b,roughness:.12,clearcoat:1});
const shineMat=new THREE.MeshBasicMaterial({color:0xffffff});
const accentMat=new THREE.MeshStandardMaterial({color:0xf4a8d2,roughness:.45,metalness:.12});
const mouthMat=new THREE.MeshStandardMaterial({color:0x75445d,roughness:.6});
function part(parent,geo,mat,pos,scale){const m=new THREE.Mesh(geo,mat);m.position.set(...pos);if(scale)m.scale.set(...scale);m.castShadow=true;m.receiveShadow=true;parent.add(m);return m;}
const torso=part(playerMesh,new THREE.SphereGeometry(.78,40,32),furMat,[0,1.05,0],[1.0,.92,.86]);
part(playerMesh,new THREE.SphereGeometry(.52,32,24),bellyMat,[0,.88,.55],[.82,.82,.36]);
const headPivot=new THREE.Group();headPivot.position.set(0,1.72,.12);playerMesh.add(headPivot);
part(headPivot,new THREE.SphereGeometry(.68,40,32),furMat,[0,.12,.08],[1.0,.96,.88]);
// Hocico y nariz
part(headPivot,new THREE.SphereGeometry(.30,28,20),bellyMat,[0,-.12,.69],[1.0,.72,.62]);
part(headPivot,new THREE.SphereGeometry(.075,18,14),mouthMat,[0,-.04,1.02],[1.25,.72,.7]);
// Ojos grandes con iris, pupila y reflejos
for(const side of [-1,1]){
 part(headPivot,new THREE.SphereGeometry(.155,24,20),eyeWhite,[side*.265,.22,.72],[.9,1.08,.48]);
 part(headPivot,new THREE.SphereGeometry(.105,22,18),pupilMat,[side*.265,.215,.81],[.78,1.0,.45]);
 part(headPivot,new THREE.SphereGeometry(.038,14,12),shineMat,[side*.295,.265,.855]);
 part(headPivot,new THREE.SphereGeometry(.095,18,14),accentMat,[side*.43,-.12,.65],[1.0,.72,.25]);
}
// Orejas flexibles
const ears=[];
for(const side of [-1,1]){const ear=new THREE.Group();ear.position.set(side*.43,.66,-.02);headPivot.add(ear);part(ear,new THREE.SphereGeometry(.25,24,18),furMat,[0,.2,0],[.72,1.5,.38]);part(ear,new THREE.SphereGeometry(.14,20,16),innerMat,[0,.22,.115],[.68,1.35,.22]);ear.rotation.z=-side*.18;ears.push(ear);}
// Patitas articuladas
const arms=[],legs=[];
for(const side of [-1,1]){
 const armPivot=new THREE.Group();armPivot.position.set(side*.58,1.2,.18);playerMesh.add(armPivot);part(armPivot,new THREE.SphereGeometry(.20,20,16),furMat,[0,-.18,.05],[.85,1.25,.8]);part(armPivot,new THREE.SphereGeometry(.19,18,14),bellyMat,[0,-.38,.12],[1.05,.58,.8]);arms.push(armPivot);
 const legPivot=new THREE.Group();legPivot.position.set(side*.38,.58,-.02);playerMesh.add(legPivot);part(legPivot,new THREE.SphereGeometry(.22,20,16),furMat,[0,-.12,0],[.9,1.15,.9]);part(legPivot,new THREE.SphereGeometry(.20,20,16),bellyMat,[0,-.27,.12],[1.15,.62,.9]);legs.push(legPivot);
}
// Cola mullida segmentada
const tailPivot=new THREE.Group();tailPivot.position.set(0,1.12,-.66);playerMesh.add(tailPivot);
part(tailPivot,new THREE.CapsuleGeometry(.19,.72,6,12),furMat,[0,.12,-.38],[1.0,1.0,1.15]).rotation.x=-.55;
part(tailPivot,new THREE.SphereGeometry(.23,20,16),accentMat,[0,.38,-.78],[1.0,1.0,1.0]);
// Mejillas y pequeños detalles luminosos
for(const side of [-1,1]){part(headPivot,new THREE.SphereGeometry(.045,12,10),shineMat,[side*.53,.02,.72]);}
scene.add(playerMesh);
const poopVisuals=new THREE.Group();scene.add(poopVisuals);
function syncPoopVisuals(count){
 while(poopVisuals.children.length>count*3){const m=poopVisuals.children[poopVisuals.children.length-1];poopVisuals.remove(m);m.geometry.dispose();m.material.dispose();}
 while(poopVisuals.children.length<count*3){const i=Math.floor(poopVisuals.children.length/3);const partIndex=poopVisuals.children.length%3;const size=[.13,.10,.075][partIndex];const mesh=new THREE.Mesh(new THREE.SphereGeometry(size,12,10),new THREE.MeshStandardMaterial({color:0x79502f,roughness:1}));mesh.position.set(-1.25+i*.72+(partIndex-1)*.09,.08+size*.45,-.35+(partIndex%2)*.12);mesh.scale.y=.72;mesh.castShadow=true;poopVisuals.add(mesh);}
}
const vipExtras=new THREE.Group();headPivot.add(vipExtras);
function applyPetCharacter(id){
 const styles={sprout:{fur:0x83d9b5,belly:0xffe4c9,accent:0xf4a8d2,inner:0xffa9c8},aurora_fox:{fur:0xffa34d,belly:0xffedcf,accent:0x8b69ff,inner:0xffc5b5},ruby_dragon:{fur:0x9d354c,belly:0xf4b17b,accent:0xffd35a,inner:0x63263d},cosmic_axolotl:{fur:0x85c9ff,belly:0xe8f5ff,accent:0xb58bff,inner:0xffa4dc}};
 const st=styles[id]||styles.sprout;furMat.color.setHex(st.fur);bellyMat.color.setHex(st.belly);accentMat.color.setHex(st.accent);innerMat.color.setHex(st.inner);
 vipExtras.clear();
 if(id==='aurora_fox'){
  for(const side of [-1,1]){const tuft=part(vipExtras,new THREE.ConeGeometry(.13,.42,8),accentMat,[side*.35,.63,-.02]);tuft.rotation.z=-side*.28;}
  const star=part(vipExtras,new THREE.OctahedronGeometry(.13),new THREE.MeshStandardMaterial({color:0xffe58a,emissive:0xffbd42,emissiveIntensity:.55}),[0,.78,.03]);
 }else if(id==='ruby_dragon'){
  for(const side of [-1,1]){const horn=part(vipExtras,new THREE.ConeGeometry(.13,.48,8),accentMat,[side*.34,.68,-.04]);horn.rotation.z=-side*.3;const wing=part(vipExtras,new THREE.ConeGeometry(.42,.85,5),accentMat,[side*.72,-.12,-.36]);wing.rotation.z=side*-.8;wing.rotation.x=-.35;}
 }else if(id==='cosmic_axolotl'){
  for(const side of [-1,1])for(let j=0;j<3;j++){const gill=part(vipExtras,new THREE.ConeGeometry(.09,.34,7),accentMat,[side*(.58+j*.07),.05-j*.13,.03]);gill.rotation.z=side*(.9+j*.12);}
 }
}
function petReact(action){const now=performance.now();playerMesh.userData.reactUntil=now+2600;playerMesh.userData.react=action;playerMesh.userData.reactStarted=now;}
// Acciones de mascota: las herramientas de supervivencia se retiran del modelo.
const toolGroup=new THREE.Group();arms[1].add(toolGroup);toolGroup.visible=false;
const spearGroup=new THREE.Group();arms[1].add(spearGroup);spearGroup.visible=false;
const woodParticles=[];const particleMat=new THREE.MeshStandardMaterial({color:0xc99a55});
function burstWood(pos){for(let i=0;i<9;i++){const m=new THREE.Mesh(new THREE.BoxGeometry(.045,.045,.12),particleMat);m.position.copy(pos);scene.add(m);woodParticles.push({mesh:m,vel:new THREE.Vector3((Math.random()-.5)*2,1+Math.random()*1.8,(Math.random()-.5)*2),life:.65});}}
async function playHarvestAnimation(obj){
 const node=obj.userData.node;const isTree=node.type==='tree';const isAnimal=node.type==='animal';const isFish=node.type==='fish';const pivot=arms[1];const old=pivot.rotation.x;harvestAnimating=true;toolGroup.visible=!(isAnimal||isFish);spearGroup.visible=(isAnimal||isFish);
 if(isTree){const target=new THREE.Vector3(obj.position.x,1.65,obj.position.z);for(let hit=0;hit<3;hit++){pivot.rotation.x=-1.85;await new Promise(r=>setTimeout(r,170));pivot.rotation.x=.25;obj.rotation.z=.045;obj.rotation.x=(Math.random()-.5)*.035;burstWood(target);await new Promise(r=>setTimeout(r,150));obj.rotation.z=0;obj.rotation.x=0;}}
 else {pivot.rotation.x=-1.65;await new Promise(r=>setTimeout(r,180));pivot.rotation.x=.35;burstWood(new THREE.Vector3(obj.position.x,.7,obj.position.z));await new Promise(r=>setTimeout(r,200));if(isAnimal){obj.rotation.y+=.35;obj.position.y=.08;}if(isFish){obj.position.y=.45;}}
 pivot.rotation.x=old;toolGroup.visible=false;spearGroup.visible=false;harvestAnimating=false;
}
// Pequeña zona de descanso visual para la mascota.
let cameraAngle=0,dragging=false,lastX=0,lastY=0;
const keys={};let moveX=0,moveY=0,sprinting=false;
// La mascota permanece en su jardín; el usuario puede girar la cámara arrastrando.
renderer.domElement.addEventListener('pointerdown',e=>{dragging=true;lastX=e.clientX;lastY=e.clientY;renderer.domElement.setPointerCapture(e.pointerId)});
renderer.domElement.addEventListener('pointermove',e=>{if(dragging){cameraAngle-=(e.clientX-lastX)*.006;lastX=e.clientX;lastY=e.clientY}});
renderer.domElement.addEventListener('pointerup',()=>dragging=false);renderer.domElement.addEventListener('pointercancel',()=>dragging=false);
function animate(){
 requestAnimationFrame(animate);
 const t=performance.now()*.001;
 const now=performance.now();
 // Un ciclo completo de día y noche dura aproximadamente cuatro minutos.
 const dayPhase=(now/240000*Math.PI*2)%(Math.PI*2);
 dayFactor=(Math.sin(dayPhase-Math.PI/2)+1)/2;
 const daylight=Math.max(.055,dayFactor);
 sun.position.set(Math.cos(dayPhase)*38,Math.max(-5,Math.sin(dayPhase)*42),18);
 sun.intensity=.12+daylight*2.8;
 sun.color.set(dayFactor>.22?0xffe4b5:0x9bbcff);
 hemi.intensity=.28+daylight*1.75;
 scene.background.set(dayFactor>.2?0x9dc4c4:0x101d35);
 scene.fog.color.copy(scene.background);
 moon.visible=dayFactor<.42;stars.material.opacity=THREE.MathUtils.clamp((.42-dayFactor)*2.8,0,1);
 cloudGroup.visible=true;
 const cloudOpacity=weather==='clear'?.22:weather==='cloudy'?.88:weather==='storm'?.98:.72;
 cloudMat.opacity=cloudOpacity;
 for(const c of cloudGroup.children){c.position.x+=c.userData.speed/60;if(c.position.x>58)c.position.x=-58;}
 rain.material.opacity=(weather==='rain'?.58:weather==='storm'?.78:0);
 const rp=rainGeo.attributes.position;
 if(weather==='rain'||weather==='storm'){for(let i=0;i<rainCount;i++){rp.array[i*3+1]-=rainSpeeds[i]/60;rp.array[i*3]-=.035;if(rp.array[i*3+1]<0){rp.array[i*3+1]=22+Math.random()*8;rp.array[i*3]=(Math.random()-.5)*75;rp.array[i*3+2]=(Math.random()-.5)*75;}}rp.needsUpdate=true;}
 if(weather==='fog'){scene.fog.near=9;scene.fog.far=48;}else if(weather==='storm'){scene.fog.near=18;scene.fog.far=78;}else{scene.fog.near=32;scene.fog.far=dayFactor<.2?78:115;}
 if(now>nextWeatherAt)chooseWeather(now);
 if(weather==='storm'&&now>lightningUntil&&Math.random()<.008){lightningUntil=now+180;scene.background.set(0xdce8f2);sun.intensity=4.8;}
 const weatherLabel=document.getElementById('worldWeather');if(weatherLabel)weatherLabel.textContent=(dayFactor>.2?'☀️ Día':'🌙 Noche')+' · '+weatherNames[weather];

 river.material.opacity=.86+Math.sin(t*1.6)*.035;river.position.y=.025+Math.sin(t*1.4)*.012;
 for(let i=0;i<fishSwimmers.length;i++){const f=fishSwimmers[i];if(f.parent){f.position.x+=Math.sin(t*.7+i)*.0008;f.position.y=.22+Math.sin(t*3+i*1.7)*.045;f.rotation.y=Math.sin(t*1.4+i)*.22;}}
 for(const obj of resourceMeshes.values()){if(obj.userData.node?.type==='animal'&&obj.userData.node.active){obj.position.y=Math.sin(t*1.8+obj.position.x)*.018;obj.rotation.y+=Math.sin(t*.45+obj.position.z)*.0007;}}
 if(grassMat.userData.shader)grassMat.userData.shader.uniforms.uWindTime.value=t*1.8;
 for(const tr of animatedTrees){if(tr.userData.node?.active){tr.rotation.z=Math.sin(t*1.2+tr.position.x)*.012;tr.rotation.x=Math.cos(t+tr.position.z)*.009;}}
 for(let i=fallingTrees.length-1;i>=0;i--){const f=fallingTrees[i];f.life-=1/60;f.mesh.rotation.z+=f.dir*.018;f.mesh.rotation.x+=.006;if(f.life<=0){scene.remove(f.mesh);fallingTrees.splice(i,1);}}
 for(let i=woodParticles.length-1;i>=0;i--){const q=woodParticles[i];q.life-=1/60;q.mesh.position.addScaledVector(q.vel,1/60);q.vel.y-=4/60;q.mesh.rotation.x+=.12;if(q.life<=0){scene.remove(q.mesh);woodParticles.splice(i,1);}}
 if(weather==='storm'&&now<lightningUntil){sun.intensity=4.8;} 
 const kx=0;
 const ky=0;
 let mx=moveX+kx,my=moveY+ky;const mag=Math.hypot(mx,my);if(mag>1){mx/=mag;my/=mag}
 const moving=mag>.08;const speed=(sprinting||keys.shift?3.4:2.0);
 if(moving){const forwardX=Math.sin(cameraAngle),forwardZ=Math.cos(cameraAngle);const rightX=Math.cos(cameraAngle),rightZ=-Math.sin(cameraAngle);const dx=(rightX*mx+forwardX*my)*speed/60,dz=(rightZ*mx+forwardZ*my)*speed/60;playerMesh.position.x=THREE.MathUtils.clamp(playerMesh.position.x+dx,-5.5,5.5);playerMesh.position.z=THREE.MathUtils.clamp(playerMesh.position.z+dz,-5.5,5.5);playerMesh.rotation.y=Math.atan2(dx,dz);}
 const stride=moving?Math.sin(t*9)*.48:Math.sin(t*2.2)*.055;
 legs[0].rotation.x=stride;legs[1].rotation.x=-stride;arms[0].rotation.x=-stride*.45;arms[1].rotation.x=stride*.45;
 torso.rotation.z=Math.sin(t*2.1)*.025;playerMesh.position.y=Math.sin(t*(moving?9:2.4))*(moving?.07:.045);
 headPivot.rotation.x=Math.sin(t*1.4)*.035;headPivot.rotation.z=Math.sin(t*1.1)*.025;
 ears[0].rotation.z=-.18+Math.sin(t*2.4)*.08;ears[1].rotation.z=.18-Math.sin(t*2.4)*.08;tailPivot.rotation.x=Math.sin(t*2.8)*.22;tailPivot.rotation.z=Math.sin(t*1.8)*.18;
 const reaction=playerMesh.userData.reactUntil>now?playerMesh.userData.react:null;
 const reactTime=(now-(playerMesh.userData.reactStarted||now))*.001;
 if(reaction==='play_pet'||reaction==='mini_game'){playerMesh.scale.setScalar(petGrowthScale*(1+Math.max(0,Math.sin(t*15))*.16));arms[0].rotation.z=Math.sin(t*15)*.72;arms[1].rotation.z=-Math.sin(t*15)*.72;legs[0].rotation.x=Math.sin(t*15)*.65;legs[1].rotation.x=-Math.sin(t*15)*.65;}
 else if(reaction==='feed_kibble'||reaction==='feed_fruit'||reaction==='feed_fish'){const chew=Math.sin(reactTime*13);headPivot.rotation.x=-.18+Math.max(0,chew)*.28;playerMesh.scale.setScalar(petGrowthScale*(1+Math.max(0,chew)*.045));arms[0].rotation.x=-.55;arms[1].rotation.x=-.55;}
 else if(reaction==='give_water'){headPivot.rotation.x=-.32+Math.sin(reactTime*8)*.06;playerMesh.scale.setScalar(petGrowthScale*(1+Math.sin(reactTime*9)*.025));}
 else if(reaction==='clean_pet'){playerMesh.scale.setScalar(petGrowthScale*(1+Math.sin(reactTime*18)*.035));headPivot.rotation.z=Math.sin(reactTime*10)*.12;}
 else if(reaction==='sleep_pet'){playerMesh.scale.setScalar(petGrowthScale*(1-Math.sin(Math.min(1,reactTime/2)*Math.PI)*.08));headPivot.rotation.x=.22;}
 else{playerMesh.scale.lerp(new THREE.Vector3(petGrowthScale,petGrowthScale,petGrowthScale),.12);arms[0].rotation.z*=.82;arms[1].rotation.z*=.82;} 
 camera.position.x=playerMesh.position.x+Math.sin(cameraAngle)*7.4;
 camera.position.z=playerMesh.position.z+Math.cos(cameraAngle)*7.4;
 camera.position.y=4.25;
 camera.lookAt(playerMesh.position.x,1.25,playerMesh.position.z);
 poopVisuals.position.set(playerMesh.position.x,0,playerMesh.position.z);
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
