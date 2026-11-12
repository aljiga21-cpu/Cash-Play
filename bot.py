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
        "telegram_chat_id": str(user["id"]),
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
    player.setdefault("telegram_chat_id", uid)
    player.setdefault("pet_alert_last_sent", {})
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


def pet_status_text(player: dict, include_details: bool = True) -> str:
    """Resume el ánimo y las necesidades actuales de la mascota para Telegram."""
    if player.get("pet_dead"):
        mood = "🕊️ Ha fallecido"
        mood_line = "Tu mascota ya no está contigo. Puedes adoptar otra desde el juego."
    elif player.get("pet_sick") or float(player.get("pet_health", 100)) < 40:
        mood = "🤒 Se encuentra mal"
        mood_line = "Necesita medicina y cuidados."
    elif float(player.get("pet_happiness", 100)) < 30:
        mood = "😢 Triste"
        mood_line = "Necesita cariño y jugar un rato."
    elif float(player.get("pet_happiness", 100)) < 55:
        mood = "😕 Algo apagada"
        mood_line = "Un poco de atención le vendrá bien."
    elif float(player.get("pet_happiness", 100)) >= 78:
        mood = "😊 Muy feliz"
        mood_line = "Está contenta y disfruta de tu compañía."
    else:
        mood = "🙂 Bien"
        mood_line = "Está tranquila."

    needs = []
    if float(player.get("pet_hunger", 100)) < 35:
        needs.append("🍖 Tiene hambre: dale comida")
    if float(player.get("pet_thirst", 100)) < 35:
        needs.append("💧 Tiene sed: dale agua")
    if float(player.get("pet_cleanliness", 100)) < 35:
        needs.append("🧼 Necesita un baño")
    if float(player.get("pet_energy", 100)) < 22:
        needs.append("😴 Está cansada: déjala dormir")
    if float(player.get("pet_happiness", 100)) < 38:
        needs.append("🎾 Está aburrida: juega con ella")
    if int(player.get("pet_poop", 0)) > 0:
        needs.append("💩 Hay que limpiar sus necesidades")
    if player.get("pet_sick"):
        needs.append("💊 Está enferma: necesita medicina")
    if float(player.get("pet_health", 100)) < 55 and not player.get("pet_dead"):
        needs.append("❤️ Su salud está baja")

    lines = ["🐾 ESTADO DE MI MASCOTA 3D", "", f"Ánimo: {mood}", mood_line]
    if needs:
        lines.extend(["", "📌 Necesita:", *[f"• {item}" for item in needs]])
    else:
        lines.extend(["", "✨ Ahora mismo no necesita nada urgente."])
    if include_details:
        lines.extend([
            "",
            f"🍖 Hambre: {round(float(player.get('pet_hunger', 100)))}%",
            f"💧 Sed: {round(float(player.get('pet_thirst', 100)))}%",
            f"😊 Felicidad: {round(float(player.get('pet_happiness', 100)))}%",
            f"🧼 Limpieza: {round(float(player.get('pet_cleanliness', 100)))}%",
            f"⚡ Energía: {round(float(player.get('pet_energy', 100)))}%",
            f"❤️ Salud: {round(float(player.get('pet_health', 100)))}%",
        ])
    return "\n".join(lines)


def pet_alerts_for(player: dict) -> dict:
    """Devuelve las necesidades que merecen un aviso, evitando mensajes por detalles leves."""
    alerts = {}
    if player.get("pet_dead"):
        alerts["dead"] = "🕊️ Tu mascota ha fallecido. Entra al juego si quieres adoptar otra."
        return alerts
    if player.get("pet_sick"):
        alerts["sick"] = "🤒 Tu mascota está enferma. Necesita medicina y cuidados cuanto antes."
    if float(player.get("pet_thirst", 100)) < 25:
        alerts["thirst"] = "💧 ¡Tu mascota tiene mucha sed! Entra a Mi Mascota 3D y dale agua."
    if float(player.get("pet_hunger", 100)) < 25:
        alerts["hunger"] = "🍖 ¡Tu mascota tiene mucha hambre! Entra a Mi Mascota 3D y dale comida."
    if float(player.get("pet_health", 100)) < 40:
        alerts["health"] = "❤️ La salud de tu mascota está baja. Necesita atención."
    if float(player.get("pet_cleanliness", 100)) < 22:
        alerts["clean"] = "🧼 Tu mascota necesita que la limpies. ¡Se está ensuciando mucho!"
    if float(player.get("pet_energy", 100)) < 15:
        alerts["sleep"] = "😴 Tu mascota está agotada. Déjala dormir un rato."
    if float(player.get("pet_happiness", 100)) < 22:
        alerts["sad"] = "🥺 Tu mascota está triste y aburrida. Necesita jugar y recibir cariño."
    if int(player.get("pet_poop", 0)) >= 2:
        alerts["poop"] = "💩 Se han acumulado sus necesidades. Entra a limpiarlas para evitar que enferme."
    return alerts


_pet_monitor_lock = threading.Lock()
_pet_monitor_started = False


def pet_notification_loop() -> None:
    """Comprueba necesidades cada cinco minutos y avisa por Telegram sin saturar."""
    while True:
        time.sleep(300)
        try:
            now = time.time()
            with store_lock:
                data = load_store()
                changed = False
                for uid, stored_player in data.get("players", {}).items():
                    if not isinstance(stored_player, dict):
                        continue
                    player = get_or_create_player(data, {
                        "id": uid,
                        "username": stored_player.get("username", ""),
                        "first_name": stored_player.get("first_name", "Amigo"),
                    })
                    chat_id = player.get("telegram_chat_id") or uid
                    active_alerts = pet_alerts_for(player)
                    sent_map = player.setdefault("pet_alert_last_sent", {})
                    ready = [
                        (key, message) for key, message in active_alerts.items()
                        if now - float(sent_map.get(key, 0) or 0) >= 6 * 3600
                    ]
                    if not ready or not chat_id:
                        continue
                    message = "🐾 Tu mascota te necesita:\n\n" + "\n".join(text for _, text in ready)
                    try:
                        send_message(chat_id, message)
                        for key, _ in ready:
                            sent_map[key] = now
                        changed = True
                        log.info("Aviso de mascota enviado a Telegram para jugador %s", uid)
                    except Exception:
                        log.exception("No se pudo enviar el aviso de mascota a %s", uid)
                if changed:
                    save_store(data)
        except Exception:
            log.exception("Error en el monitor de necesidades de mascotas")


def start_pet_notification_monitor() -> None:
    global _pet_monitor_started
    if _pet_monitor_started:
        return
    with _pet_monitor_lock:
        if _pet_monitor_started:
            return
        thread = threading.Thread(target=pet_notification_loop, name="pet-notifications", daemon=True)
        thread.start()
        _pet_monitor_started = True
        log.info("Monitor de avisos de mascotas iniciado (cada 5 minutos)")


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
            player = get_or_create_player(data, user)
            player["telegram_chat_id"] = str(chat_id)
            save_store(data)
        send_message(
            chat_id,
            "🐾 ¡Bienvenido a MI MASCOTA 3D!\n\n"
            "Cuida de tu mascota, dale comida y agua, juega con ella y acompáñala mientras crece.\n"
            "También te escribiré por Telegram cuando tenga una necesidad importante.\n\n"
            + pet_status_text(player),
            mini_app_keyboard(),
        )
    elif text.startswith("/estado") or text.startswith("/status"):
        with store_lock:
            data = load_store()
            player = get_or_create_player(data, user)
            player["telegram_chat_id"] = str(chat_id)
            save_store(data)
        send_message(chat_id, pet_status_text(player), mini_app_keyboard())
    elif text.startswith("/help"):
        send_message(
            chat_id,
            "Comandos disponibles:\n/start - Abrir el juego\n/estado - Ver ánimo y necesidades\n/help - Ayuda\n\n"
            "Te avisaré automáticamente por Telegram cuando tu mascota necesite cuidados importantes."
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
.gameHeader{position:fixed;z-index:1;top:0;left:0;right:0;height:92px;overflow:hidden;background:#241c35;box-shadow:0 5px 18px #0006}.gameHeader img{width:100%;height:100%;object-fit:cover;object-position:center 48%;display:block}.hud{position:fixed;z-index:2;left:10px;right:10px;top:100px;display:flex;gap:8px;align-items:flex-start;justify-content:space-between;pointer-events:none}
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
#actionBanner{position:fixed;z-index:3;left:50%;top:47%;transform:translate(-50%,-50%);padding:10px 16px;border-radius:18px;background:rgba(28,38,66,.78);border:1px solid #ffffff55;box-shadow:0 8px 28px #10152b55;color:#fff;font-weight:900;font-size:14px;letter-spacing:.2px;opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;backdrop-filter:blur(8px)}#actionBanner.show{opacity:1;transform:translate(-50%,-58%)}
#hint{position:fixed;z-index:2;left:50%;bottom:104px;transform:translateX(-50%);background:#07120f99;padding:7px 12px;border-radius:20px;font-size:11px;color:#e5eee8;white-space:nowrap;pointer-events:none;max-width:90vw;overflow:hidden;text-overflow:ellipsis}
@media(max-width:600px){.gameHeader{height:76px}.hud{left:7px;right:7px;top:82px;gap:6px}.panel{padding:8px}.petStats{min-width:145px;font-size:10px}.petName{font-size:15px}.petActions{gap:5px;bottom:max(8px,env(safe-area-inset-bottom));width:98vw}.petActions button{min-width:76px;min-height:38px;padding:8px 5px;font-size:10px}#hint{bottom:104px;font-size:10px}.gameChoices{grid-template-columns:1fr}.gameChoices button{min-height:52px}.gameSheet{max-height:88vh;padding:13px}.sickAlert{top:118px;font-size:11px}}
@media(min-width:700px){#hint{bottom:94px}.hud{top:100px;max-width:900px;left:50%;right:auto;transform:translateX(-50%);width:calc(100% - 24px)}.petActions{bottom:22px}}
@media(orientation:landscape) and (max-height:500px){.petActions{width:70vw;left:auto;right:10px;transform:none;bottom:8px}.petStats{min-width:160px}#hint{bottom:70px}}
</style>
</head>
<body>
<div class="gameHeader"><img alt="Mi Mascota 3D" src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAgMDAwMDBAcFBAQEBAkGBwUHCgkLCwoJCgoMDREODAwQDAoKDhQPEBESExMTCw4UFhQSFhESExL/2wBDAQMDAwQEBAgFBQgSDAoMEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhISEhL/wAARCAF7BLADASIAAhEBAxEB/8QAHQAAAQUBAQEBAAAAAAAAAAAABgIDBAUHAQgACf/EAFUQAAECBAQDBQQFBwcJBwQDAQECAwAEBREGEiExB0FREyIyYXEIFIGRFSNCUqEXJDOSk7HBFjRDYnKC0SU1NkVTVHOD4RhERlVj8PEJJlaiZHSEsv/EABwBAAEFAQEBAAAAAAAAAAAAAAIBAwQFBgAHCP/EAEIRAAEDAgQDBgMHBAECBQUBAAEAAgMEEQUSITETQVEGIjJhcZEUgaEjQlKxwdHhFRYz8AckYkNyorLSNFOCksLx/9oADAMBAAIRAxEAPwD3bxn9oKR4cPGmUhhFTrhRmU2T9XLjlm8/KPOlY9qriIp5bktPyEulR0bSxoICcSVV+t1moVCdWXJideK3FE3ubAQK1M2SoG2sUFRVSOdobBaOCiiY0XFytRk/ap4kPglyqygt0Yiez7TPEN1Xeq7HwZH+EYjTVeK/WLmT3ipkq6gPIDz7q0hpKcsBLB7LcGPaIx6pjMqqMEgf7KHaf7ROOZlyzlSY0P8Asv8ApGXy7lpW3lH1Icu+bcjEF9dUh3+Q+6lNoqYj/GPZbhJ8c8YuKs5UJc/8uDPDnFDEdSl3FzM42VI2siMIkHPrBeNLwTZco/bkIgSYlV30lPunnYfShv8AjHstKp+Pq1MDvzKP1YmJxnVzOy7ZmElDiwFd3lAjRRcHrFkVBE/KH/1BFjBXVJLbvO45qtlo6cA2YNui1xt1woBJGoHKHO3c6i8QWpkFCbH7IhYdF7Kj0fKsNmCddmn0jRQ+UVU9U55lN2nQPhE51YKbXiunRnRYWvD8AF9QmJicuhVBMYqqzKyO3At5Qz/LOr8phP6sdqcmdwIplpyki3zjQQxQPb4B7LNzy1DHeM+6uP5a1i36dH6sX9UxFPS9BMyy6kPBq97c7QCwTVs2wyf+D/CKjHY2RxNLBb0VzgEr5ZXCQ3Gm6zh7i5ijvZZxoEH7kXtPx/iCZlW3HZxJUoa92MxVdSl/2jBrRm7SDV+kVXZb7WpeJe8Lc/VXXawCGkYYu6b8vRE7WOa4N5pP6sPDHVbP/eU/qwPjS8OJ2Ebp1LT/AIB7Lz34uo/+4fdETeN6xzmEfqxKYxlVXFWU+i39mBhMTZMXuTvEeSlgt4B7J+GrnLhd590QnFdUO0wkf3Ya/lZV7/zlJ/uxWXjgFzDIp4R9weylmomP3z7opolbqE+y6X3QVpOhAgdxFjCtU+bU1LTKQB/Vi8wmkdm96wM4vY/yms6WtGJx8Fj3GM215ei2eBEPjbnF9Oaj0XHtcm6qiXmpkKQeQTF/MYoqbTqgl8AD+rATQkgV5s2gqnm7urjOieYR3LitA6GHPbKFV1XiNXZVVmZpA9UxQTHF7EraiEzjIt/6cMYgRd6BGfQErV1ilqcQqWyWEh91b09DTOZqweyLPyyYoyn88YuP/TgenuPGMWZjI3PsAX/2cUCnLBVtIF6gq83c84BuIVTmj7Q+6eOH0oJ+zHstC/LtjRQ0qLI/5cMp48Y2z2+kmtP/AE4BQe7DQF1GLmlqZ3A3efdV89JANmD2Wlt8c8Zm16iwf+XEtHHDF2maeYP/AC4zRrRNtIkIVY6xNbPL+IqA6nhv4QtJHG3Fitp5j9nHFca8Xcp5j9nGeJVrp+MLCr7Wh4TydU26ni/CEfflrxdf+fs/s4V+WnFxP+cGf2cAGeFZxyghNJ1SCGP8IWhN8ZcWE96oM/s4spLi7idwjtZxlQ5/VxmSFWifLTGQC3wht8sltHFOshhvq0LXpPiZXX03Myyf7kS1cQa4lskTDV/7MZZK1LIBc2PWJblZUW8ub4xXOlqb+M+6l/DQH7g9kSVTitiaXKuwnWQB1bgem+NWMGkEpqDAP/Cijn5rtAepihnz3DfpE2nlmtq4pmWCEbNHsiljjtjNayDUWT6txPRxvxh/v0uf+VGWMG7xi2Z5RMifI51rqLLFE1hOULQPy24wI0npf9lCDxsxhf8An7P7KBAZMuoG3KElpJ8rxZta9h1F1VOdE8aaIxPG3F/KfZ/Zwk8a8YH/AFg0P+XAb7uCDeG1MkbWI6xIY5vMKM9h5FG35asYf+ZNfsxH35a8YD/WLX7OAfKQNRb1hsmxiS1rDsFGJI3R0eNWMf8AzJr9nH35asYn/WTf7MQC36x8NILI3okzG6OTxqxiCAak38GxH35bMY3/AM4t/sxAPfS0JWNjCZG9EoJR3+WzGH/mLf7MR8rjXjAbVFr9mIBNRvHM173gSxvRECUcnjbjEH/OTX7MR9+W/GP/AJi1+zEAatYb1B1gco6IrrQDxzxgP9YM/soSeOeMjtUGP2UANgr4R3s77CEyjolujs8c8ZD/AFiz+zhs8csaWJ+k2Rb/ANMQBupyEgw2o90wDmhG1G6uPONQT/lNn9mISePeNuVTZ/ZRnrhAhFwL+cMKUGN6LQVce8cD/WrQ/wCUP8IQePmOR/rVr9kIz4m8IUb7wPJKGtvstCPtAY5A/wA6Mfsh/hHP+0Jjkf6zl/2UZyrumG1G40gblFkb0WkH2iMc20qct+yhCvaIx3yqcuP+VGaKNvKEKNjeOJKQMb0WkK9onHoBtVZf9jEZftH4/B0q0vb/AIMZ2tWhtER06aRwJS5G9Fpo9pTH52qst+x/6Qoe0jj871SX/YiMoSrvWEOhUEkDG9Fqf/aPx8f9bMD/AJI/wj4e0bj4n/OzH7If4RlwN9oUnWOC7ht6LU0+0Xj4/wCtWP2I/wAIdHtEY8I0qzH7Ef4RlqdAL7Q4jU7wSQsb0Wnp9oTHh/1sz+yELHtA47P+tmv2Q/wjNEaQ8nTfWOSZG9FpbfH3HR3qrX7If4Q+3x7xud6o1+yEZog3iQjlpCpcjei0kcdsbH/WbX7IQocdMan/AFo3+zEZ4kwsK10hQkyN6LQU8csan/Wjf7If4Q+jjhjPnU2j/wAsRnrfeMPIGsLZDlbfZaEnjdjEj/OLP7MQ6njZjAj/ADi1+zEZ6lVtodTcC0cAuLW9EfjjTi8/6xa/Zw4ONOLv/MGv2cZ+DaFpI5wWUJMo6I/HGfFx2n2f2cd/LPi7/f2f2cAYNoUFDnCWCUNajn8s2LudQZ/Zx9+WfF3+/s/s4Brx9mhCEuVvRHX5aMW/7+z+zj78tOLf9+Z/ZwC36R9eOslDW9EdnjRi7lPMfs4SOM+Luc+z+zgGCusfX1hCuyN6I7/LNi3/AH9n9nHTxmxb/v7P7MQCHSFgjnCFKGN6I4PGXFv+/tfsxH35ZMW/7+3+oIBr23j68CiyN6I5/LJi3b6Qb/Uj4cYsW3/zi2f+XALmhaTYxxXZG9Ec/lhxZ/5g3+zjv5YMW/7+3+zEBGcDeElxaiAjaGJJ2sT0dOHckbucYMXNozGoN+nZiGW+MmMnli080E9eziiVKtGXStRClWiLog2A0isEsspuSQrEQQxi2UFHCOLWKyBmn2/g3CvytYp5zyP1IDEK7sLEcZXjmlEER+6EZflbxR/vzX6kdHFjFB/783+pAekfKFZhHGaQ80op4vwhFv5WMU/7+3+zELTxWxQf+/N/qQGlVoW2vSG3zSW8SJtNDfwhGX5VcTkC083+pHRxSxQraeR+pAihSVGxIBiXLthZAQDEZ9VI37xT7aOI/dHsiQcTMVq1E8k6adyKw8XMYsT3ZvTQKCdD2cXdAw8JsBShcnQDpBFNcOx2JdKUlQF9torpa6rf/juQE8IKKM2kAufJDbfFLElhmnG7+aIWeKOJLaTjf6kVlWoQlnCkjKoHlzigmGnWD3TcQkeIzkauPunTh9PyaPZFyuKOJrfz1sf3IZXxWxMkaTzX7OAxU6W9HAQYaM4he5/HaJTa2Q/fPumXUMQ+4PZGC+LmKR4Z5n9nDDnGDFaNffmv2cCS1pUdLQw+2txHcBI52h34uX8ZTfwkP4B7I2TxfxUQCJ9r9nEZ3jJi1L2VM+zb/hwIsE5bHcRGmD9fbnCGqmP3iuFJB+AeyOxxgxYT/nBr9nChxfxXpefb/ZwDph3NeONTN+IrvhYPwD2Rt+V7Fdv5+1+zj78r2K/9/a/ZwFixhYGukD8TN+IrvhIPwBGY4u4q/wB+a/Zx8eLuKv8Afmv2cB+XSEk2hDVT/jKIUkH4B7IzHFzFXOea/Zx8eLuKb/z5v9nAQt5LYutQA9YiPVdpGjd1q8oB1bKBq8+6cbQwnZg9loI4uYp3M83+pEOd45YkkxlE6hx07JCIBELmZ5Wt20Qr6GSHg4TcjrEKfEKst+zcVLhw+kB77R7I8pnFrG819bMzsu22dh2esWn5VcTn/vzXwRAYyv6sAC1ha0PtpzEQMdXUMbYyE/NI+jp3HSMD5IsHFLE52nkX/sQv8p2J8pV78nQfcgcalb6/jDjss4lhfZtrWopsABvAyYlO0Xzn3XNoKcm2Qeyl0njBiuamJhD08g5Fd0ZOUWp4qYmT/wB8b/UjOpGjVWVni6ZN0IWddIv3aZMKAzjLf5xDpsYnc2xkN/UqTNhdOHaMHsFfu8XcSNeKdaH9yIj3GLFatJabbPq3FOKQE6qGY87whTASbARLGI1J++fdMHD6YfcHsvPTzlwok7neKKreE2MXC1d035GKSqEZTaLp5VW0aKFTVeL16xeygJIMUVKFwv1ggkVAHXURVyHvlWUQswIgYH5vboIbpC7Pn+1DrKbsXG2WI9LH16r/AHorZD31NYO6jCnqu4I03AgKmpgX5Rl9M/SAxqnD+xRMg/discLn5qRJoxFNHFgfWJr6imblifviINJuFqHK5idNaPy/9sRb0+7fUfmqefn6LQGZshCfQQ8JvXW8VKVqCE2+6IUH1DmY9i4QIXlxlN7KxVNEk/4w0p3N1MRA8TqYcQu++8EIwEJkulONduMo35Q0KEHSVOga7CHkOZVXiUia2BhTI9vhQiON3iCH5rDq0K+rvvtE/EbJbw6U8w1b8IsnJlP2je0Q8UrC6G4R9yK/FJnvhGbkp2GQsjmJZzWAKTZxY6qMGtObU3JshWhywKdilK1LvurX5wZMOpdYaKdggRD7IvvVPHl+qndsB/0bPVLKdY74dto4IUBePRCvNUpJvE6U0F9ogC8TpM3hqXwp6HxKSBeFgWhI0hURSptkR4T8D3rFHi1v89Vz0i8wme696xU4oTeeVfpGHx/xu9R+S2+Af42+iGKE2PptF4J55NnnIHaPpWm7dYJ6imzqrdIy7v8AD81pAftUA15rNMD1gSrDORw+cGdaTeZHrAxiFsJULcxGYrD9qtDS+AIQmbpJHWBufNpqCqfby/GBapJtNAnnCQnQKS9dSbCEJ8Rj4HuwgKsoiNDR7lU9VsFMbGXaHQbGI7R1HnEhJtFgFXu1KdCiTCgrpDZUANI6DppDgQuCczwtJhmHAYMJtPJV/wC7w80/l9IiZo6lWxvC2ulurJMwRsY6ZsnSK8OFOvKOh3mNIAxhGJCpD0wTrf0ivnXDkI6+cOrWSb8ogzq7J0g2tshLrqLLfpdYt2De14ppRV3LjeLmXIuLRIpx9oExUn7MqXeFDUw3qNYcTyi+AuVnCV8tRSfWGs5HnEktle8JMv5RKa0WsVFc430Td84sRpDbjHdJG0S0SxIuI6pvu2UIQxN5aIhM7mqtQJOsdt3biJqWL8odRIlSdB+EDkeBui4jCdlUhwXtfWPs1/jEh6QKXtU2vCvo4kHW0R4p3PcW22UqWBrGB191HvmGp22hCrgw67KLaOpuL8oaJKDYw9ntuEyBfZJIMfAEnXaPj5R1JtC5gk1XyUi+kfdolCrHeH2ZVbqSUi8QpiSeQ+M4IiPJKAdFIihzDVfTauYiIV90xOellKSAd4bMn3DbeGpahjd1IipnuGgVUo6xwxKdlSkb7Q0lrMdYjidh2KkmnkHJMKMNqO8OPJKCQYjqVeHA4EaJstIOq44b6wydYWq5Gm8ItbeARWTSjl1hpa4W4bCGVC++0KkskKN9ojuai0SBpDLxBJhAiUQGyocSqGzoY7fWDQp0KvDidoZSbHzh1BvttHBcnkG2374ebTzuYZSm0PIvpChJzT7YudYfB9YZSNodTBLrJ5uJKF6aRFbNyYlNkZfOOJSWT6NYdG0MoNoeSq5EKkT6DYCFJVrCBtYGPgbH0hUltVIbVc769IfRtEVvqIfS5pC3SEJ08oUF2AAhsKChpC0i1oK6BOg3hQ6Q2mFg9IRGAug9YVCb847cgQiWyUBcxw/uj4cvOPuUIlSRvH19YSpWUGONuBRgMwvZFlNrp5JuYWnzhvOL6bw40gumwBMNyTMYNSnI4XPOgXHAL90wyZhKV5FGyjFvLUlTgGhuYhTtAcVN3Rcjr0ismxItOgVjFQBwNymxbePjnWbIvaLJFEUhoHUmGHGlNCyk7dINlWJdL2QPpeHra6ipZtYrN4cBttCFPDncRwui14ktyAaKK7Od1KbmL90n8Y+UrvXGsVnv6O2yDfrE9t4FAJOkR3vaToVJYxwGoUts5heHkjaIbUwlPOHBN62SLmIryApTQSpl7Qgq58osabRH6kyVBKrEaWEMTVCflSoPBYt1iAaxmfKFLbTutdVzsyBtqYbQ+4tVhpeH1SqW73F7xwICToLCCdmduUjcoUmTRrqbkxf05oBQI5QPyqildhF5JTFkgjaIk45J+M81p+EVNsoZUq1jv5QbTcy2mUUpShltprvGP0usKlU2vdJ3EWruJipuwUpVtkmG4K4wRuZbdQqnDzNKH3UfEy0qf5Ztz6QGTygVaDQ7xb1GdL7ilrOpOv8AhFDMu5iTtf8AdEGEFWrRlaAoEykKGouIr3pZC9tDEx1dzpe3KGFKG0TwwEJvOQVAMi4FXQsnyiwZxI1SWRLzUvmUrTNFnQqWajMpQbDMbawXYg4Vy6KemYQsdokXN4i1GdouzkjZJHcB53WTT9SV2hWw2QF65YrXKq4ld3EEQQT8mmVdyDcGxivelkq8QBhxoeQCCiJYNLKGmtqNrIvEtuqIcOoI6w4iVaKfAIW1INqOqdPKHbSAbpq8Z5Jbc83pvEtMyCglCVGw6Q/I0tknVEHFGwaZyU7RLfdI7thEaed7BpqUQazd2gWZy1ZW6taFtEKB0ELfdmndEIy3jRlcN3pftJhTQCuWnKKCep3YlQy2I3iHFUyk2fopWWF2seqD/oxx45phw+kPNyjbHhSD6xZPM5DrDBSDyiY3LumX5k23dJuIeCyTzj5IHMQ+hHOOc5c0Jcvc2vfyi0k2cx10/jEBhNlXi1lVWAiHI5SGhXdIkRMPtoI0J1EaRTcPIcSlLbaBYb2gIwqR72FK5CNTpE0hAFyACBErDYY55LSbKnxWaSMdxVVQw6GGyco+AgNr1MSlHaNptbRVo0+rzbfu5TmSSrpAJWVjsli299IPF6OCF32aj4XVTPIzIDfTkNorZpA5RZThu4TyiumDFSx1xdaUjVeZnDYK9YpKsoZDYxcLVdJvsYo6raxt8Y1kizzNkzR7nN0vBBKoAtA7RFeP1ghlRf4RVP8AGVYxeAIjl/5v8Ii0v+cK/tQ/Km8ufSGqdpMH1iul0epzNWospfdcEalw+7wmPSMspZusRqfD42W+P6sVu5+aem8KLqUPrF3HOJU2qz7GmyxEam27Vdtrw5POZXWr7hUWsH3fUKom5ozStJQkXGwj4Zeu0Dn0mtKrco6qqrULbR7qyjkyheOSV0QcURlaUgm4htucSVW+UD30ou1lQ2maVnzAn0g20Ttbpl1c24sipc2nKSN+UQl1Iov3gPWKc1By2sRlulZJUTBx0VvEglrx91XDlVI+1eLGsvlzDyiebX8IE8xgmqOuHVW5M/wiqx2BrIW2VrgE7pJ3X8liSptRUtPLMYM6KrPItE9IASbOr/tGD2gn/JzXpFT2UaG1L/T9VedrzekZ6/orC/K0dGkc3MLAuI3xXnFl0CJkkLmIgHdH4RMkUk7/ABhqU91OwjvKUBeFDXfSJDMqVaq2ESUMD7SREF0oCsmxEqxwmLpe9Yq8TXM+v0i9w+gI7UJFhFNiVIE6rzEYrHXXe4+a2eBjKxo8kN0hNqyi/WCWpp+tPpA/SU5q4j1glqibOH0jNuH2JWh/8UIFrg/O0+sD2I2wEpI5wRV0fnSPWKLESLtoI1jKVv8AkK0NL4Qg+pNWRfygPqY/OAdoNqmn6mAuqD68XhIfCFLduVH+zCPtGFjUaw2fGbRoqLcqnqxoFJavz6xJBiM3tfpEhOsWIVeU4BprDgtbQQ2NtYcTa0OBAV8oWjqDmGkLZlnZpeSXaceXa+VtJJt1g64b4Mp1Vm6onFzLrTLEoVsqWCkJV97zh1rblN68kCaga6Ax9zjS53hxQ6pTwjB06tVWlWS6+04dJpI+75xmqkltRCgQpJIUDyI5QtkTmuYcrhY+a+zaR9m0hJj4noNOccgK7e8V88uyTfWJ6torp89wwbRqkJUaTV34vJbvWihkvHBBTklTiQnW8Owf5EFT/jKmpaJG1/hDqG7DUfhFozT7IFxbSFqkwkf+9I0bcoWVcXFVpBTbSHkNldgBe/lCXU2Vryi2ojSHX057WEETkBcgaMxAS2cPOLlw4sEX20ism5VTSihY1HlvGmoZZMrl5DaBDEEkFO3bHOK+lrHOlIdsp1TSgR3buFQyUn2iwCNzvBTKUPLL5gjX0iPQadmdCnE6X2g+p8kHWwmw2hjEq/K7I1PYfR3GdyyqrU8pdAQjn0jjFFmHkXbbPraNQnMMtLXmKdjD8rTWZZu2UeflFMyvexxLVcvpWSMDSsaqFPXLghaCCPKKN4AEg/O0aziyntWKkpFz+MZjVmENrOXmdovaOsMzO8NVS1NLw3d1V2bSOpTmUABvDZOvlD0rq6ImOPdUdg74RFR2ChrMEhVvKPp9hDpuE5SPKJtISlTdhz3h+bkM2oGpiksM5JV1chtghd9sBOgiLawPpF3UJTskHyikeOXaGZ3DNdS6dpyWUB9O5iEFWVFnMuF1oJsBl59YqVkhyxhhpUhzSN0iaNxe0QHCOUT37EaxDUxm2ifE6zVBmbdyZz2htboBsRElUsoC9jEB8EK9IIvDtigDS3dLUUrG/wAYZVYbQkXMcKSTYaxzcw5pSGkbLhIA3ER1969tYms0x6YVYA2MWicMrQyVFJ23MA+oazQlEylc/ZCqrovePhrEyfklMrIUDpETLaJLHhwuoz2FrrL5JsYfa1HnEe9ofl2yuwEGSAhAUlO2msPINhDiJFYSCATpC1yxTygBK1EY3dF8nXWHUDUaQhphRIAES2pVRI0gjI0BcI3E7LiNxYQ9tyhaJXKdeUSPdQRcQHGaiMLk03sIfQknb8I4hm+g+UHtIwMxMUFMyXLTK9QL8oj1NYyIXJUilonyusEEpQoa2NoUE3MEUxRTKtfXJAV93rFTNSXu6kkEEL5dISCsY9waCjqKGSNhcQo6EEq0+MWbNHmHWQtDRKTsbQiUl8zgPIbxrGFaXPVWipRI01byNg8ruJ+F94fq5ZGMBYLlRKdkbnd82CyJxhbCiFpIPmI+Cr7xoOKsFT9MYLtTkXGEnZ0d5PzEA5lLqsOusHBMXsu4WKGaENd3TcJrPlTryjgdSryi4kaC5NpNkkiOTmHFyyb5do41LQ610TYCReyqi4LbjSG/ekA2vDxkwm94Zcp6DqNDBPdJa7ULWsB7yd7dCgLHXoIUhecd3WEsyqAm1rERZU+TDzoShNyTtAZnNBc4osrHGzQq5+XdWj6tJhlmVdA72kHcxhZ+XlkuOtlKVjQmB2bYLDhSRYiIUT2SvJBUp4fGy1lCYlO93jcdYtpBhOcWiA2bqsIs5JQSofhBzNa0aJIXOcdUXYZo4qcyls6J5xoB4bSqmMxRZRGhtAVhKoiScS5oSk3IjUW8ZSplr31tt0jOvMbnu4hU6UzsDeEFmlfw+mm9ohIBCeXSAmfYAGmvwjQcU1NMz2qrglw6QBz6wQbdYOhv9U/Me6M26onWU31EMLYSoWAMTHSDpyhDQClaxo2tGXVU5JzKCzRA4vMAd+kWK6Z2aADfzgyw3Q2JprM4sAkbRCxFIJkl2SbgbGKtrmcWwCnkO4e6G26ekCJsnINh1JOvrDQXbaHmnggg3hyWMWQxvN1uHD2nSZp4UUIKyNbiKriDJSrDyuxCdRygIo+LZimt5GHco5awzVcQuzxUt9wqUdyTFE9pIDMux3U2OIiYy59CNlRzoCXFAcjEPnpHJmazqJuLRDVPpQdCCYn5g0argwuOgVghWVV4mszOSxGo5iB/6T6CPhU1g90WiLLNH1UmOCTojOWnwdj/ANIkGftztAa1U1p12MLNWWr7UVr6mO6mNpX2RHMzYUdT6jrFbMzSbHMR/hFQ7UFnXNEF2bKgbqgBVAbBOfCk81aqnW89gb/xhPaZlEiBF+oqZm8ubQ7RfyMz27QUDy1ifTzCRtwoU8XDNkTUepGSdStOmU6eUFdVx45MyAaCtbWMZ4lZSNDrHC6o7kwUkIcmQ4aaLs677w8VnmdoiPaI2h47w29qgwYFguvcr5vwiJTAyjXXpERCtBeJcub7xztkrVd0pIVbN1jbcGOsiRZBAtaMPp7gSQP/AGYNqDiNUgjL4kdOkQOKYZg8i4S1EBnhyDdapiF5luSIATmIjGK7lXMvFNrXggqmKzMtFLdxcakmBKemQoHW94CrqfiZs4Fgkw+kNMyzjqqGbTe+kQlC0TZlWYm0RFC4sYNilOKQDYw6366QzoTaFIJBgjskBU9k6gRZMHLp1ipbN7Wiwl15iLxFkCfaUXUFYbRm58oJ5asFtIBv6QDSU77ugBIuImpqqjyERA97DdqZmpxIdUWTFZK0nJ+PKKOoz5Layo3UdorHams+GwiA/OlSjmVqYGR73nvFLDSNYbhNPO3vfTqIrn3MxPlDs09c90xCcWNbw4xtgpZK83uEBvWKOqnQxcvKASddIoKqohJjWSbrOs2TVDVbP6wSyagDY9IGKKre3WCKVVqIqJD3yrKH/GESSZs0oDpe8Ip384PrCZFZKVA7ER9TTeYN+sV83iU2PwospY+sjU+HxHaP/wBmMsphsvWNPwAsB1653TFbsfmn5vCjGm3L6wnrD8+Ehbd980RKY5aYXy1h2pODtWyD9qLOnO3qPzVTMN/RPHRRjt4azEkmO6mPo2PwBeATf5HeqUSbwpKrQkDrCrCDTa7cncwq+kJAjh5wK4pWsFFQH/24r/g/wgXSYLZ5N8On/g/wig7Q/wCFq0XZr/O75fmsAWrKty/3jB9QDmpjJHSAF42ddBGgUf3wf4csaYz6RRdlj/1L/T9QtH2t/wDpGev6KxtzhaBfePspvC0i0bwledgLibg+sWNPTdd9IhJsTFnT2zobRHmdZik07bvCtkCyRpsIVHyUkgR3LyipJVyArSgjR2KXEus+fSLuhaJdijxQbVA26RlsZ8Tlp8G8LfmqGln/AC42B1glq4+s+EC1LV/lxF+sFVWvn16Rnj/hKvv/ABQgOv6TSB5xTYhH1CPSLfEBtMo9Yp8QG7DfmIyVd4ytHS+EIVqR+pgJqn6cesG1TH1GsBNVJDwgYvCFLO5UcAQ2RZRhxOovCTfMY0dFuVUVWwTzV7RJRpaIzavlD4ItFiFXuTiVXhSTrpzhCNPWO35iHGoCtI4NszUrWZ+rp7JumyMsUTTrguLkaJHnF9O42aqgmmZ9CewdbKWAjQjpeMywviybwvMuqYQmakZpOWcknPA+n/GCaUYwviGZvSKq/RxYqclZrUIHMIMPOzloDFPwuWije41Oh0sbbW/VWOAFWxSH3l2bpco46uy7ZhbQRnc7Nifn5uZSMqZh9a0p6C5grdxTh+m0SqM4dE8atMj3cOvEFKmxufjAS2LABOwEc1uVoahxarjqqkyR3tYDXy//ANTptaFWsIQN/OOhV94IBVZK4s7xWz50iwWdfhpFZUFEJ0Crc9IJui7LdNSOq4JKSPrUwL09RLmu3IwTU5wM2W4pKEpHeUo2AhyC2e6Gqb3LI2YGZsXPKPnkEC4GkUf8radKpAffWlIGqiwrL84speuS802lbDjbqFDuqQq4MXTJGuPdN1RTU0sQBkYW36ghQJr9IbxNoyXO2HZ39YhzZC3ipI0MX2G0DS4vEic2iKgwN+2CIpMr7MBcQqogKOn/AMR9ibEkjg+gzNVqqsrDFkoQN3XFGyUj1MQ38O12cpstO4nr0jh5M80HmpJhGZxCFajN52igDsrtBqtRTYfLVMBBABNhe+p8gASVYUdJChod4M5CabYbGxvGH4pw3MMUqacwvjCozNWlwXGW1ps24BuPWKbhZxonMVszNOrpS3W6abOW07ZI526xEnjMz77K2kwSekpDNfMBvoQR52PJekH55JTcket4HqriaUpySqamGWU9XHALwDVbHpo1EnZ6bWC3KMldr+JX2U/GKim0Cns4dp9ax3IKrdfriTMBl1whuUZPhAAO9rQ18HlPeK7DKY1gu0G17eZO/lsN1c17HlJmEECpSHoXtoDZ95Mye0aWhxCtUrSq4PpBEy1h90htWFKahpZyne4B+MYXKYoawjxKqWHH1KRTJp28qlSr9io8h5RYUMjY3hp2Kk4p2XlNM+WMnM0XsbajnsVoJNj68oW0ohY5RwtkEg8vxhaU2N4vjay89bdFlCcuEjmYJS2FI2ECtAVokmClCyUiKOo0cVdQ6tVHXGsrRta0CMyLXMGdZUFIUN4EZtvTTrEKTUFWUOllAIuIq39HYulNkIJ8opnwS4YBifk1TDovvHEgAi8fPHL6wx2trxMb4bKGfEpT76EpsAIpnz2jhIFhEh1ZKrHYwxa5hY2BqSR1wm0txJlm0FXe3hlw2tCEulKhaDdchC211ouFKdLvqTnAtfbpGjzGCZaZphcZtfL84ybC08WVpJOo3jSUYqLVPKc+hT12jO1TJM6t43DKLLKcY0ZMjMKTodTAfMS4Qm4tBTjGsCZmVnNz+cB7sznO+0XNEH5BdV9XkLimiAIs6dkukG3nFSXNYkMLUg3G8WEjbhQWHKUdy6JcS11EbbxXvZCokWt6xTMTqko+tWEp5kwds8IsUztPl5xMtJyzM03nZ96dAWpPW19BFeGFjrXuriGkknYHNAA21IH5oZStCTyh9Mynlb1ioxxI1HhyqSXitmWblZ9zs2piWczAK6KhLD4WkKQrMkgEKB3EPMaHpqrpp6QNMjbA7HcFXRmBmJG/rCkTV7gRWIcvoIksqJMSOEAFW8UlytZQXWCY2DDVGXVsONTco8klk5VNg6iMjkE57Wg7wZWpugPKVL3W07o40dlRT4hEJW2PJW9DM6E3bzVw/hCdqSHXyRlbSSFX3jOplBRNuJcNylRBF402v8Qn10xyWk5ZEslwWJG4jKZlw9qVE5rkknqYXDaUMOZJidc+VuQ6LQOGuH5SsTU3O1g2pNGa7ecP3zyR8TEev8Q6pX5pTrUwuQkUG0pJy6siG0Da9ucLk6gaVwIfdZNnKxUil5Q5pHKM/VNhQsD3bRYVDrustX2KweKSF1XI0E3sL8gN/crR8B8Sp2XrbNIrzy6hRKur3d1qYVmLSjoFJ+MVtaoTtKxRP0qXZmZgyzv1fZtlV0HaAdhwpnGHSvIGHUuZr9De0Hs9x3r8zNZqWJaRQlAQFpaBWu3Mm0IySw1UvH+yclTUtko2AXHe5C/IomoEs5JpCJ1h1hShoHUZbxMrMuwZRRsAq0UuGOMztTmEU/H3ZTcjNqDaZgJCVy6jsb9I+xcqYotTekJhWcIAU04NnWzsYhyU7nvzMKyddQzYa8R1LbXGhGoKE55CUvKCbWvvERW5tD0w7nUb8+URiohVhFxE0hgBWakcHOJC6gWUbxd0R9Eu8lYAuDzikSq51h5t0tquDAzx52WSxOyOutNq2MET1KblktpSUbqjP6m8HHja14b98WU7xDUsqJJ1iDS0nCddSZpw5tglJVlN4lsTABBva/4RBhSFWiZJHcJqN9kSSdTLFik2Ii0RiBfIkfHeA9DxAtuIeTNfECKiaia43srKOqIG6vZupqdJUpV7+cVE1MlSt9tojuTWhtqYjKdvz9YegpgxNyzlycUu+8dbuDpDSDmOvwh4GwixtooBOquqdVnJNNkk2PnCKjUlzx7xNhFYlwgaGOhZN7xG+HaHZk8JjayXew8oZemA2QL/AIx1SzFTPPEPpHWGKjuqTAMxVsmbHI/jCHZoncnQdYgJKt4StaiNoiuj1UprgnJh+yTY6mKhcx2au8TE5wFSDeK8Ndq8Mw5xX1rCcoCsKN4AcUr3xVr2PrHUzKlC6QTeJrsolTYAj5LaG0i0RzRDmU78d5KOHXVDYw8guHreHwtCRytCg8gcx5Qgo2BIaxyjqbcUNSbRHGgVfl5xYFwG5FgYq31EOKv4YaqIQxuifp5i9yG6u+ROpsTa8FFBd+rSL6EQG1pVpkG/OCPDiypCSTpA4f4V1duixJ0hN+9CQokCPrxZEKrThN9YZfHdhd7bQl89zWAdsjbukNjQExIZc1iMg6CF3y6pgSiGis2H8huYntVDKNCfnFEmY01haHwb2NoYfFdONfZXpqKlA3VtEVybzXN94ru2O1zaEKcJ9Ya4QCc4l1KcfBJsb/GGS5mMM5tdecdBvBhoCQm6cFt4Uld9oSIUlG1hCFKE+yFEg3/GLJg5QB1/CIDegESmnL7xFeE81WjblraxIDlkxWtu2EOBy/OGC1OXS36nkfDeU2POIrr2d03P4wtakKN7C45xWTM2lE7kJsOsM8Mg6p0PB2UtbmhudIiuuX2/fHVuZhvDC1amHgEC88uEFBvA/VnNCN4vnRds3gfqqdDb4xppN1nm+FNUQ+K/WCOX5EQNUY2zesEsp3gIqJfGVaReAK/p2qCeVrR9TlfnhHnH1PX9WU+UJp6gJxQ84gSm5U1osEV05f1kaXgZz6020uIy6RVZ0+caFgqZyOKvpYRXX1t5p6UaI5l5gNTCrm2sdqExd5q50vFImdzzJOm8P1V+6Gyk8oktkIII6hQ3RA6FGEvTi82FNzDJvrlzbQ4aa+3fuhY6pN4xR2pzjU44Gph1IvyUdIsZTE1Ulx9XOOg8rmPV4O18tgHM0XnFR2NiJJbJqtUKLHUbco5lPnf0jMf5XVNs394vc66RJYx3UmFhSihwecWkfaynI7zCFUydjqkeF4PutENwLR8BASjiM+pSS9LINukTGuJDN/ziTPmUmJbO09Cd7j5KK/spXjax+aK0ixvBfO/6PD/gfwjMU49pS03yOIJi+c4m01+lFhKrEN5Rcb6RBxfFaWphAjddT8HwespZiZWWBWXTH84e/tGNBw0f8lsnyjOXn+2mHFJGilEiDzDNTlvo1ptTqErSNQYrOzUrI6p2Y2uP1Vt2ohe+kaGi9j+iISDH2X8Y4y+04O64g/GJsm20q6lLBJ843RqI97rz8U8l7Ftk3KSqnV6iyRBDJyuVA01iNLtoSRlt6RbMgJbGXaK+onzbKxpoAzdJyWGu45QlQBFxDpF9/hDFiCQfgIijVSnaKzoZv2sUOKD+fH0i9opsXLRQYmNp0nyjM41o5y0uC6taUPUr/PqL9YK6wbK+ECFMXauoHUwX1Y974Rnx/hKvnf5QgHEGswk+cVNc7zCPIRbYiP1ybdYqquLyyL9IyVd4ytFSeEIYqKby9+kAlY0fHSDuqaS/rAHWT9cNIGDXRS3JhPhEJv3zePknuiPr982jRUW5VTVDQJ5vXyh8WiOg8okIN4sgq8pad4cSkHfSG07wu+kONQOXxTbbSEqQF+IAnrChCstyLaX39IcBTJCYAIUdvUnaG3KrJy+js01f7qbqP7os+GEtRMaP4kqeJe3mKbhxwNNSLKsvbrPNXlB0zWJCTQlNCw/TJNAF0FTeZXoYMC+pK0EGBveLOBvpfYDUX3Pl5LNZSqyk8SmTmG3VJ3SDr8odU8B8doD+NtDcwtU5fF2GUe7sOuD3xhvwtr5m3QxYUzEcvWKM3VJZSey7IrcA+wsC5BhWlM4rgxpcj4yS13XcHoijCtKcxpU6mwxPN0+n0VrtJ+cUnNlvskDrFwjB2GLZpmdqlQUOQGQK+UD/AAdSpPCKcqD36XE9aN7/AGm0Ej5QWN2A0tb0hbCwutXR4YyNjgNhpsOQF9bX3usc4k1McNsUyT0gHFUadsHWXDct30uDBzwvrFLxJjKeFQSJ2mYfkUzIlwdJl1Y7mbyBgN9pqQDuDZadSAVSztjpyjJ+GfEocPcSSdRmUqdkKrKe6zaQdbp2I9IbvYkBP1WEQSyRytFiQR0+fr5r15WMSOVDuuykk22pOjKGRlCTyjB6riRXDfii1LyylCh1cJU5LE6MrV9pPQQVTHGXCLcp2xnn3BluGktnMfK8eeeImN3ca4ocqSGVMMJs3KtcwAdD6mEZKWODgdVaz4RTzUj4ZGd0j68ivZiEhxCVAhQUAQeoI0gmw23lRciArCjzjuHKUqYFnTKozA77CC+kTiGUZTveNXJ3o185FojmIvsSgn2mn0o4f08bf5YYNv7wgzxjMh6ryyld4insAE627kZj7UU7nwJI5T4aoyf/ANhBrXJovzsoonVVOY//AOIoahuWRw9P1Xq/Y9ofSRnoX/8A8phT6kLC0gBSTcRgfEtmY4a8SJPEdISUyU8c6gnaxPfSfibxu6SVDTeBbiXhX+VuD52SQjNMy6TMSvUqA1HyvEYkraGNjmuY8XBFj6KDXZwY3qOEaDSyVS+IZtEy8R/sUd43jW8STTc9VXiwAJeXSJdkcglIt/CME9lNuZn6hXa9UsxlsNSQkZDMP0bpuFCNiS9mbSTudVeZMG9+bXqs/g+GikBjH3b+51/Ky6EhJSeQN48t8dEmmcZmHDcJW424D8RHqFbtkG1tRHmf2pG+xxzRJtIt2zKR6kGGhoVoWNJDgeYK3CXV24QUjxtJUPlHyJpl5wtyfbTroNi3KNlZBih4VvS3EiuJpc1NFik0WnomKqptVnHza4bT5RsrOLEykumWwzJytKk0d1ltpoZyBzJ6xaS1pGjV5bh/ZV0usgPW21hyudd+gHzQnJtYgk2wtjCtUeRvdXdPyvC3uIbNIFsTUes0gDdx1nMgfGLuYqtSmFFbs5N/BVoQKtUAgtuvJm2lCym5hAWlQ6bRBc8vNyfyWrj7NUoZlLB8nOB+tx9FEla1IYjlTM0ibam2SNSg3I+EV0yyCCANSdIzjiZLq4YViSxXhJoylPnHQ1UpFJ+rST9oDlBlTqr/AC0qtIo9CcDb1c76ngf5vLgXWv1A2honWyo6/BpKWZgZqx2xPK29/RSWGl1KZVJ0iWmalNJ8TUq3myH+sdhCJ/h9ihrv/QaUX5OP2PyjRahimWoEr9DYEaRTqXKfVl5CfrZpQ0UtSvM6/GBOan5ucWVKmZlwndRWYQsa3nqrqiwEyNDpGgA9b3+drAemqz+tU6q0ROetUaflmhu82jtED1MVjL6JlsOy7iHW1HRSDp6Rq0nXJyQ7vbqII/RvjMlXzjCuJGLKfR+LkmxR2kSjc+2E1JhHg7Q+FaRyMORvsbXSV/ZkGJ7oRYtF99Db11HuUQKQSdOZ/GHJSSfnpr3eQl5icmObUs3nUPXlBZgXBRxjPzaph9UjQ6UAqqT43F9mW+qz+4xoQxE1Q5QSOCZFmjSCdAQgF5wdVK6w659tlR4dgzqg98fpb1OvsBf0WVq4bYrca7ROHJ/LbmAD8rwOVCVepEyJery8xT3ibBMyjKCfIxtrNaqCl5nKhNFR378Sp6Zar0gun4nl2KpJvDKoOo76L80q6iA4h6q0l7PQ2ta3oT+u/wBFjdPm/d+oIiZO4hV2RQ2o5iNoz56st4exzVsMLme3YlCVyT6zqG/uq9I1PAXD9msUlOI8cOOyNAUoiVl0aPT5HTomALWuGayqJMKqoajgkdNet9refks6qD6pp1QSorVfVKAVEfKKt19Muqz6lMnl2iCm/wCEejUYjkqUgNYZoVOkGU6IKmwpRHmYYmMQGpoLNapdMn2F6LbWyBp5GCExGgV2zsrmbeS9/Uflr+a8+oWSU3FwdiNbxYsNlQ11vtBNjfhqxT5d6s8P0PIZaBXOUdw5gBzU2YFsJ1KUxJX6FSpKYCVVh3K4oDvMIHi9DvEhtQCNd1QVnZyohmDY+8089reo/wBvyT7DPvWKsNUxQBTPVJsOpuNUg7R6Jx1OrmcV1FJWvspYpZaRewQkJGgEVqsDcPaHX5KoUujTUzP0dzMxMvO3C3B9oiGapOKnp6ZmnfHMuFagOsRXh1z5rXYNSGNsYLSA1p36kg/kFiPtRzWTCtGaRfvTV7E3gf4bTrs7hWVVMqKloUUJJ5JGwjXcd8MKXxPl5KXrtTnKY3JrzJVLfaPnA/W+FMtwzwa/O4fxEqqS8gO0XLTSLFQ52PWFgdkdmOyXtDRvrKdtPEO9e40P5qE1e8S2+Qinw3WWMQUhuoSpSllSLrCj+j9YNcPYKruKZb3mh04pkz4ZybV2aFjqkHUiLF0rQ2915yzDaovLMlrGxvoAfUr6jqGcAkecHVLdZabBuD0MDzvC7EEsAUVyhMuD+jyEj98Rn6LjChtl2bp0rVZYbu05yygOuXeKmaMyHyVzHh9REy9gfmrDEE5nfIQR52gcfcBPzvDKMQytSKuxcIdBspp0ZVpPQg84Qp3MrSLWnja1gAWeqmyNlIkFj5rQ6Mx/KDgdWqdLd6cos37wlA3KDuYyyXmS4hKuo1jROEtZFGxjLNzRHuFWSZObSdiFaA/MwI41w8rCWMatSSkhMtMEtXG6DsflDNSyxuvT/wDjuva+KWjduO8PQ6H6291CK7psdukdBskEbGIKZttvRxxtNuRULxaUek1HETyWaBTZ6oOKOnZtEJHqraIxBOy9DkDYmlzzYDmdAmXVqcQllhJW7MOJbbSBqpROlo2Dim4WKnRpN1QM1JUttEyRyVYaGIuHMHyPCxaK7jl6Wn8QNp/ybSGFBSJdR2Ws7XECtQqUxVahMTtRc7SZm3C46rz6DyiZTQFvecvGe2mOU9fMyGnOZrL3dyJPTyHVIUq+8JvcecJG5jpNjEuyxQXQbQvPfyhvnHxhCFydz3EIzaxwL5Qnc6QBFiiunQdYcy9IaTpvDyDHWXAr65Aj7tIUQDHHZV1pScw8Q084jySRs8RspEbXu8ISS5frHMuaFhopUEkWUeRj4gg2ttygY5Y3+E3SyMe0d4LrehhzNyhsHXTlC0i59YdJsmQljyhQOkfAWhKzrYAnyAvCEhEF9vrFTPC80L9YuGm1K1SlRSftBOkRZymvl1KlMuAG1u6bqHlFfVEGysKbRJHhhGS50iVMSb8kUom2lNKIuAoamG0ouNBDW6cBTKkWQoW5RAbbs98YtVJsIgJT9d6GINV4mqdSG7XBN1Sc91ZJ5xAZqPbS+Y/GHq3KKea3trrFTLsKSwUpvpDL5O9ZAWEC6d+kyF2vp++JjMx2mxigW04HbEWud4uaZK5FAqNz0iM2QtNinuEZNQp7a1A63hqZJN/SLDsk2BEQplOptsBB1Lvs0/SsIegmu6TA9YJMN37BNzA9Xk/Xg+cEGHjdpN4iYf4VIrt0VNm6YXcfKGmlAWELvreLMqsCWDeG5k92FhQURDU4bI9YbfsjbuvkEER1SoZaXdAvCs3WOAXXSxsYWg222hpJGXzhQPTaEIS3ThWbiFFemm8Mn90dC+UAQiungb7iFg62hnfaO5rQBajBUkQ4Dp0iMF3AhYXttDRCcClJXYQ+ldiCNIgpXz/CHAvoYZc1GCrAOlPrHS70OkQO1I2McLxtr8obyJwFTPeNdPnA9MvZ6yEq26RYl4Ab6QPTMwRWkmAe1G0omU6mGXHLnunSIxdBvHA8AbH5woakusGeWQg2ihqYOQ3i9V4TeKarKBSQBtGgfuqJvhUOkq0V6wSSRumBakLParA2EE8koFAI+UVMo75VnCe4FfU29yPKOSH89V6wqlWKtekMtLLc8bfeiulOqnsGiKpE/W6wbYWStTqgi+2sBFPGZy45xp2BpZJS4TqbRW31UiQd0KUhpQWTvEl3M/kRudhEthm7qiRfWFol8k8yORWInRtuQq+R9gqZ7A06pxTpQqytdobODpwDRKv1Y2+TkjNBtCBe6RrBLLYYYS2O0SCTvcR6VJ2eijaCJbX8lg4+0Mr3EGPbzXmGYwvPM69nc+kQvombSbKYVHqKo4Ql3EktpFuloFZ7Djcu4e0aA87QkXZ9z/BMPmEsnaHh6viPyKwF2VmGD9Y2oQgIc3KF29I2uYoMos95tJ+ERl4aklf0Q+UOO7NVw2c0+6bb2qoj4muCx4A7H5GOgW6GNXXg2ScuezA+EC+L8IGmhK5XuoVrFfW4ZVUbA6UaHorOgxikrHFkZ180JhZTtDvalIuklJ8jHUskDXfnBTScGfSMgl9ThGblDNNTzVDssbblSaqqhp25pXWCG2anMNeB5Y+MWUlW5v7Dyx8Yuf5AZQR2ph6WwKtB7rt4mGhro943KD/UcPkOkjVyRxJPIsO1KvWCqn4smexGYg23ijRhF1Gyony1CmUIAAvaGnfGsGgcPdLeik5tPsiJjFK12C0g3iX9OJWdUH4QOsUx9onMhUSUsuJT3kqjm1tYw+IoDRUb/uhEUniJuVuQkm/KK6q1FM+92iQQOkV5WWx39PWGlPgIJSQfQxGqKqWU98qRBSxxeAWTMgoJrrZ84MKob2P9WABh1Saq2See8Gk6/wBpLtq/qxEDvs3BSnN+0agvEi7Pp9Yq6uu8qj0ibiRYDgvveKWqzN5dA8oydcftFoqQdwKlqTmZm3zgFrRAe84Mp1wFg+UBda/S35R1PuVKeNAo6Vd0RzNZZj5BukdYSPGbxoaLcqpqtlISOYiSgXF4jpFh1iQhWkWYVaU6BpCraQlO3rC0p0hwICvraR3KVJIG5Bj4CFDcesOBNOVN7PD/AGU1xHpatzkeAg/S5ZKDb7IjNuDCvceNmK5FRsmeppUBfcgRojavqh1Fx+MLyC9RoDnYHH7zWH/02/RR61Iy9cpc3TqkkKlp1stqv9k8lR5VXUqnwynMQ4fn0KW0+0Wm0nnmuErT8xHqx3UG4+EAnELhkOIM5Q5mRLbVTp06guqXoHmARcH4QtiVOlgY+Oztr39kaUSmnD+BMGUZWjkpTu3eA+8uxiehVkx2szAma1MLbt2TKEMtAbBKRaGM9hrp0gnG5XQxkQtvudT6nX9UKcX6cKzw/qjFrlLeceVo8w4Np0vi+Qp2GDlZqs5VmmpSaVs0larKJ+EewahIioyE1LKFw8ypNvhHjjCi10HiZS0i6TI4gbSbm1gFw2SAhq2O4ALDq0r1Ix7L3C+iIUzV5+sVmea7r621FCCsHW1uV4ynjbw0w3w7qFIqmEUzQpjq8z0q+oqIUk8j5x6MqIKahMWOhXmHx1jIvaQlS/geWft+gmQD5XhJDpoEFMyTMDJI5197nTUdFo2Hao1UqLITcsChmZYSpCfui20EtOdQof8AvSMk4LVg1Xh9TCpWZcuC0dfONLlH+ysTGrheXxtd1C8AxOn+GrJYh91x/NAXtOJKuHaXRszOoV+MGj7geZpTnNdKljf+5AN7Rj5meF86R9l9CrfGDVooXTqEpBBBo8tt/YEU9ffjH0H6r0/sMb0A/wDM78mqSym4h9KezUldgcv46RQ4wq01QMJ1OpUxKVTMi0HUhQ3SPEPlE7DFflsV4WptXkVJU1OshagDfKu2o+cQOa2jr78k3QKFTcFYdqVPoBUTWqgqdmydkqOuUeUPImD9kXH7o+mjkSTGZYyxguT4mYKw/LzBaTPzJM4lJ8QtoDCE7BSIYWta53qStO7TtBGBe1ZK5ZXDk+R+icKFGN7cQWZ6ZaOnZukDyEZB7UssHuG6HrXMvMpI+cC46ImNu6w5j9Fh2COI1Q4aYsbnxns4Al9onR5rr52jScde1StLYlsDthpTibqecSVqBPIAXitwPwpTxodkaYlxMqxLSwenqkR/NGhuB5mN5wrgXAnDuVTLYLoEtNKa0XUZ9IccfPNWsOPiA1uq+jxF8wDGw3c3nytyv+i8uyHG/iHTXk1Va6i7KNrCnO1lldmpPMbR7Kw1WUYkwxSKwltTQqksl3IoWynnoYlNYrcVLBiZlKUqWVuy5LpCTHJqqKnVtqKGmW20ZUNNJypQPIQ20NBNinpXVUjwJGAW5g/wEJ8a5JuocL6024AezbzjyIjHvZaxXMHGs83OPZltUhbcpc6pFtbRsHFB5S+HVeG5EsY8Y4YxbOYLrkhWKarK7LmxB8LiSdUn1ggLm6SRjeE1r9rn62XuNMzmSnXkL/xjGuOCcfy9WYm8LNTb1AbbBJkdVoXzKh0gswVxSomNZdCpCZblpterkq6qxCudvKDhuZelDmYWppfzB+HOAuCrCaN4aMmhXmrDftHVulNLlq80mfLSClPapyOJVyuDGbfT09ibGiqhMq7ScmJgLQm/O/dT+Metcb4Bw1xHlVN4nprUtPZfqqjJpCHEHkTbcRgUtgWS4bYtpFJqS3Zirzlelw3Mq8D0vc2KfO28E0XOhUWpq3xREOjsTppsvZ8lIpwrhCjYaYVcSzCZieWP6eYWAcyutgbfCKyqz0rSpCZnqk6GZaUbLjzh+yB/GPp2fVMVWYUs65gPgBYRl/tI1R2U4Yltkke+TqEOEHdPSOkfpdQqOjLAxh56n1OpQrP+1k1L1BaabQ+2p6FWClqIW4L7wW1H2lcLS+E/pWnPuOT62yEU9Q7yF25+UeXqLg+qYlbccpobQ2yLBS+fkIqprDtbZnDLJpTjr97DJsrWFDXEKRJV4UJCzOLt31Wj8FcOzXGXjXJs1V9TbKlqnqm7fZhOoR8do9c4mrqa1VT7qkNU+RHYSLKfC22nS4HnHnH2SZVyi1PG07OpLU43KolQhR1R1EbfKpJQ0n0v5wR0bZORR8epdUHYABvz1J/IKoxhxCouBpdt3ED6wp4/Vsti61RKwDxCoOPu1TQX1mYZF1yzossDqI8tcaKrNVfiRUEgqWZZQZZbvoIm8Epqo0PirQ0OIWh6aX2TiRspJhvW11KnbGBlv3rXXtBlSWLHQkD5jmI82YoXIcIfaFpVWeSWKJULrJTs0tW59I9D1Nwysy83axbWRaPMvtcOZ5OkO6KcQo29OkFbooQiDmF56L0LQuI2HsV1F6ToFVbnJpIU4WhuU9YuiUrNxrHlD2MwZvGuIJuZRb3amKCD02j1HTpgLZSSdT/jBXI3SRgWJaNrKcCylYSt5hC98q1gGM+48VBun8NqiouNAuWSAlYJN4xz2k61MM8Q2Eyk0+z2UpqG3CkRjtUxBOTzHZzc9MzKBayCsqB+ENuN9FMigtaQuWp+z5WJOdxLRqLiOY7GkTs+FTeZVgoJFwk+RIEe0KxWZmrvrUAJSRa7svLpPZttIG2kfm/UqXP4Zp1HnJgrZaqCS4hQ0LawrS/nGos4g4qcTKTLtU2nVqZk2mggOJQWQvzvzgsxGllWsp6ed4nDwN99r31PqvV7r8o64W2ZqUdc5pS6CYUzNOySs0o86wsb5VGPGs/w24n4daXUXKDWWEy4LnatPFZTYXvYR6P4V4hqOKOHFIq1eZdam3SppztUFCl5dMxBhGvdexFlL4LC3RwcNk3xgo3vtIdxFTW0s1OSt732Qy9sjkuw533gawFiUYnpIeUR27JyOgcz1jTqgttdHqIfAU0ZRzODz7pjzxwMmyjEFUkgbtOozp8rRKpZbSW6rM9qMMjlw90oHej5+XRbfLEtFC2jlcbWFoPRQ1EaRiCocPsfPydZxkmqNVlmXSzNMS2iXikWBvGcIFk2h9J0HWLNzA4arzKlramkk4tO8sdqLjexRnK1rAtGObD2DUzDifA7OuFX4GE1HibXZtssSCpajy1rdlJNBBt6iBIHTeFAEnb0hAxo2CSorKqpN55HP9SSuuuKccLjq1uOr8S1qzKPxhFrQooKD3vxjt+7eCTC7ew5Ry9xHCDpHQLecclXRHx12jtrx9uYSyVJAsY6lOpMdtrHwG8A5KCljTeHWUlagkbqNheG2klZAAJUdgOcEErJNUmUMxPZc5F7KOiBFViuKRUMJkfvyCn0FA+qlDW7c1WvS4kk55t1Iy6lI5wJ4h4nUqRSpjtiFC4svT5GB/idxWTT23GpJKFAX+sUdo8w4w4mOVd9xL5aWTcXBtaPKarFsQxOS5NmjovRqPC6SijuRr5rfJ3jemUmeyU6l1u/dN9U/GDbCvE2n18ttzSw2tdghd9z5x4fkanNT06mXb7RxS1d1A1vHqXg1w1mVoYmKqHMxCVJB2t5x0c9Vh8gkjkOb6H1CcngpauMsezT8lvDNMceKSFNpQoXz3ic3SpRKhmUpS08wfFHZGQblWAhGZYSNifDHyplKFkE94eE9Yn1XajEJrWcGDy/dU9PgNHHe4Lj5qb20khuyGEm1+7zvE2kBT002qRlGlrbF03TcEc4paUw5OVNCGLZ1L1Sra3ONTpVOakSoMo7MFN0/wBU8xC4W+trH53yGwK7EG01KzK1guQpchLU5UsCmSZa7VOVxsp08zD7qKehaXSyx2zSMjeYaJERSrKFX0vFG+45OTpbQe40bqjW8ZzRbcrMiJriTfRMV7DFPqS1PTkyULWblwnU+QEDMxhOTsW6ZNKcf6K2ggn5YBxTjpU50138hAvV5tyjvDIrIpwd1N9opa/FamkGcDuq4o6GKo7ubVVVSo01Tz+dtZR95OoimLJExflF43XnFXStRd+8FG4iJNBibXeUUht6/hvooxBZ2ojmsJRlKsG4K+LVpuFXTUt7wkpuBEFFODWl7xZPMvNGzySPPcH0MQ3llAuraLBlQXnMgdC0CyrZ2Quq6BDbLLrax0EPPzSiecR1TpQdTbrAvnGZE2EBuisgtZHKG3LhBJ6RVu1TIN9PWIyqyDe6o59Q5wtZLHCGm6qa6oFz4xeYfNmknkYG6s+mYWOz66wS4fTZlN+kSqBpDdVGriM2iJ2iLXMO5vuxGQrQXh5CtPKLEhVicCrnpDU+bN/uhaVW5wxUFWZhtw0RNOqSyboELOsRpZd0C/KH7877QYGiE7pQNjC79IZzBWxj4K1jrJbp4qJ22jqd/OGSrXeIs/U2qe2FPqteGX2aLlOMBcbBWXaZbxwrtvA2vF0sNjfprEdeM5dJNgdIimrg/EpbaSc/dRal28OB0W0gJONWidBpDTmN7HugeWsMOrqb8SdbRVH4UeB4C20L7WwuD6xna8eKSNAkfGG149mAkqARlER3YjTDmn24fUHktI7cW0hJfJNh84EqHixFVYCl2CuducXKJwL2VpEthD2hw2UVwLTYqwW9beB6cd/yqkgxYOTIOhN/OKSafvPXgJW7IojuiJD1xqbGPi5aK1mauLGHS75i8FlQFyyBwABVusUdUPdNuf4xcum4PrFRU02bVf4xcvVQNgqml6OrPWCanquBeBimn65QHxgmkDbfpFVL4irKHwhEdLP1nwhr/vqt/FC6SPrBHyk2m125mKuXdWMeyJ6SknLGsYBRdDo/qxlFIUcojV+HmpdH9WK8jUpyU6BEctL6kkc4TMtkTkvlH2xaLVuXV2d0ARDcRmm5cHX6waxZxjb5Kqc7UrZqBIplpNpahmUpAi2U8Eb79IrZZ/spRkA/YH7oQ7N87x6uA59iV5u7KwlWKpkK/wAIrak0h9slQBERjOG+htCDN5rhRvfSJEcLgbhMPmaRYobnJfsXVAbcoiFBEXszKKmV93lziuflFsKssemkXsMwIAJ1Wfnhs4kDRREp0MMYzp/vNHbIGoTE9KNf4RIr6M1H2/o/4RVY73oAPNW2Ad2ckLCFJtmHQxpeFUf5EZjNHVfXuj+sY1DCPeorPpFP2aNqh3p+oV72pbekb6qwKReHEAJt1h/ILbQgix2jb5rrz/hgFKSMw84uJGVuwCRe+8V8nLKdIsO71tBDKMZWwDp/GIFXKALKyo4STdQ1y4Ty05Q0WBc3APlFuZcH/CI7krlvbYxCbIx2hU4xObqFWzVLampBzu2N94En6eaeSgkm8aHLs5mVpULC8CWJWwia00tGUxiJnFc5q1WESuMQa4oSBP0ij1gtS/nYyK1IGkCeW1SRbnBA9dm1/tJvGfkuGEq9bYuAQzidXfSfPeB2su5ZZJi9xIcwSeRMDlcP5qi3SMxX+NaGiHdVQ+72kuq28CVZVdYtBOsfm5tvAtVj39esdANSnpDsmWvAPMRy/eMcaVYdY+3WeUaCgOpVVVjRSWz1h9s2iM2rpD6FRbBVhUhMOBWluUMJMOAwYQFPC3SPjoISD0j4+HU6wYTZCE8LLFM9pKmLWrIio01xHqbRpx7inEn7Dih+JjGcXPfRnFrAtQCsgVMFlavIxr82+EVKbSNg6fkYLkvTcD+0pInf9tvYn90l+dl259iSccCZqZSVNo+8IcLBvpdJG3KMy471GZoNFouIqQSJujzoUP66L6pMaDhXFMljagylXpa0lqbQC4kHVty2qTCtcrg5mjyUxtGVxIXtfWBHh7ipWK5bFZWvOuiVTsU25IJtBDiyopo2HqjNkhJYl1EHobR599ljEMw9XMZyKnLiooTMm/MhRN4X7yF+Y5SDzC9KSqx2gvsTHjzikz/J7ijP5EhCW6g3MpIHVUer2JpSUIUTqQDHmP2n5cyuMGpxsACclND5pgHtuE7LGWsddes5iYDyGX+T8syv5oEZzxzT73wyqmUXLK0uX9IvsM1Y1PBuG5nNmL9NRmPUpsIrOIbPv+Ba8wNbyS1AeYEK5N08X2YKDfZrmg7gp9u9yzN29LpvG2s5VJFo88+zC/8A5PrMoTqh5LlvKwEei5WUGUEnppGion3gavCu1UWTFpvM3+izvj61m4XVa32QlR+cEdCfSrC+HHQb56SwL+iRFdxxkg/wqxAQdGpYq/fDGFHFHh1hN4knPTGxf4CK7ET9oD5Lc/8AHozUr2n8X6Kzxk8HcHVpCbHNJLsPhGJeytxB91VN4TqTv1bq1OyBUdj9pPzjXK88pyhVJvXvyqxb4R4npM3PU2rKm6YtbL1PmitLifsG/OK4g3W/qWcIAO2K/QSaUjKouGyUAlfkBHjyt4sXWOO9OqQWSiVqyG2z0SNIJ6n7QmIa1RlSxZlJbtWcjr6PEsW1MZbSJF33j6dXf3eWnm8qyPErMLwIfmOnJFVRup4AHEd4gL3TWJkN1aYIGjll/MRlfH3884ZVO4zdkM4HpB9Vagl9Uq62Qe2lW1X66QG8SZX6RwLWGTrmlybczCO2Ummh7jSen6JHABlNJ4JySpb+c1xzPNOc1IGyfSCqpOu0+kzs1LoLrsrLqW2gC9yBGWezHi9qoYT/AJOzjiUzVOWVMJUfGi/KNxShDYv3T1Bhx5uVEow2GDK0b6rxXVMf1qszTr87VZpLrjhCGGicwVfRISOcewsDSdSp+AsPDEiHWqi/L9o4h3xhB2KvOE02k4XoNWVU5LC9KVUyrMJh1sEJV94DrE2p12Zn5szFRczvO63PIdLchA5QBvqmmRVEk4JFmi/Pf5Kr4jqJwDXRfQypjy3w7wi5j9teFpCXS7O1gp7GYI0lUpJKnCegGset25iWmWVtTqUOsOIUHEK1Ck2jDOAlVp1Exhj2TkXG2Z4v9jTQs2IYv30p+EFGcp1S1VM+RuQD/T+yEcf+zhijh0pc/hCe/lRJSigl16UOV9BA1IHMXgn4AcTcT4hxPI4ZqUjP1BuZOVZdYKVyoH2lEjYRryJt6UUlTLi2FD8fXrFtTMST0upZlVy0u46nK460yEuLHS8c7K43ITjqaohiyRvBHn+aedl8j7qAbhpak362Noxn2j8lNGFanYB6nT4dQq2oAOojZm3s2ibKWf8A3cx5o9pbGcvWaxK02mOJeZpVw4pJuFOHcD0gGtsU5bM0h21tV6Zlag1UG2J6WWFtTbKHEqHmkX/GKjiLhNPEPB81RkPIYmlkOybi/CHRsD5GMg4AcTmZukow3V3w3My5JkXHFWDifuX6xsKp5QJQo5SORhSLrhBmboVk+F8J1mgUkUypYcqxqKFZA1LoCkOq5EK6GCLEfCXFOG8NM1tU1JKrKT2jtCbVdbLe+/NVuUHzGJanLIyS8+8hOw6j0MV7kw4+8HS6tTlypTi1a25kmJHFIAWei7Jwid0j3aHXqsn4D1tFRxlipDncdqDaXS0dCFcxG8SDFktqtqmxtHlEYzksL8Ypms0UAyqJgpeCdnEnRRHpHrClVGVqlOYnqa6l2Vmk521pN7X5GGS651V7EODFwxsF574u4Afw1xATXfd3X6PU1hfvDaCsMr5hQEHvBTBDWJeIjOJWJZxGHsNMFx2deQUpdeI0Qi+8aoKguUCgEMutq8TTyApCvhDc3imbmWG5VZZYk2jdEtLoCGwepA3hxjsosqWqwuepqBIHW5b/AKdUmpvqm5h6YUO884V26Ax5e9qSptuV+k01JBcYRnWOkejahX5SlyT8/U3UNSsqkrWtRtcjYCPGGL6xMcRceOTaUqUuoTAQynfK2DpAtvdXb2mOndyAC1X2RmCiaxfNgWAlg38THoWm3Q22DuLRhXs1pFJrGLqXcZilOnXLvG4pX2OXW1toFxuU3TC8J815o4xMprHGZyXfupoZEK9COfSCtrBVKk7BmnsJyAKDhAUB53g/r/CPCmMcWSldrc7PU5bah7+zLHSbSOXkYBPaFRJYMkFu8P35uUps82WTLPnMU6bgw5HIGDvBUGNYXVV0zWwuLQBruB7o54XYCotSkHMb43lG5+Rl3SxQKY4O44sHvOqHQEWg6nsaVKaT2ck6iRl0jutS6QhKB8IoqAtt3AuGWZVV5ZinJyAbZjYqPzgb4ms1JWAqunDaXHJ/JfK14yjnl84R0jraK1wrCoKWAF4zEfT0H1KKE8RTJuFt2utKWdFNreBCvI6xNmsTOVWXZbLbTTTWqA0kBOvS0eCqXhuv4nqaKdSJGtzdSmXAhDXZKBCjpdR5AR7dkKBMYZpNKpNRWlydp8i01NKSb/WAd7WGw9xB6KfA+mlePs7O35JeJqv7jhWrvE2CZVQ+YIjEeASO1rVSmjrlaCb+saNxZmlSXDqrLH28qPxgB9n5nsqTPTBH6VwJv6Q9SNvMFR9rX8LCpLfeIC2pK7jSHkq0F4hod7um8PoVmA11i6Xi5UlKrQ6ly20RgqFgmOIShSXphTwSFAWHMQgq0tzhtKtNo7fpA2ARalODQQrYwhOl7wrl1jkiUT05R9cQkAkgAEknTzixlqJNPqBdbLLe5Uo8oj1NXDTszyuAHmnYaeSV2VguVBt84lStMfm+8hORA3WvQCLYtSVNTdCA6sDVa9vWKmZxF274aQrNc2QlOgUYwmLduIWAspRc9StZhvZWWQh05sOivqYwwwtKJVvtHBqXlcusA3FrGiaZIrallArI0B5+Z8oMFzop1OWSMzruirdenpHkbj7jZ2bnZmUYe7JtvSYeB5/dEYWeqnrpQ2RxJO619LSxUwLmtsAsl4m45XOTTqFzanBc3Sk2CYyo1BydmEMy2Zx11eVtI1JJivxpWUsrXmOljYdfMxsHsq8H5jE1UbrlbQooUbsIUnQJ+9GtbBDQUXFd8vMqnfUS1VVw2/PyC3T2feDc3LUqVncRsMmYJzJJTdSAY9TUmnilS6Wy2A3oRlHOIGG6UzSJRppgXyiySRFwqcAOXc/d6xj3ScRxkduVbEEAMbsEh10ocKm9L/jFZMvnOUW31HlD70yAq6BdP3b6xWzbwUe4dzofOIzndCn2N6oswMw3NPrU4SS0q7auYPnGnIeBTmXvbXzjJ+HjrrVRcUUFTLwspXIGNKLgS2NdB+6N12fsKUELI42D8SQnXXbgqOxG0QJdIZbUbauG5PlHXnwAEg+KGX5gCwSbJSPxi7uL3VSAbWUWouKCczXjI7vlGX4vmGpeYuHFuvub5joIPcQ1lmnya3X1ZbjRI3MYvXqkuoTDjhGQq38hGQ7SVLMvDB1WpwCncXZzsnXago90LyoA7yhyhInltKCgtKUK8NzraKh2aCQm6C45bup2CPMxBmJhSjmW0tZO6wYxThda1qN2sSpaSELc7RJ2Sq20Sm5dFVAVIuJTmFyhZ2jMpmaQ2wVKS9rzB1gfneIztFYWGHlFPU7xPo8QqoO7GbjoVDqKGGUXOh6rbVUenyif8ozXavL0ShJ8JjgwxKdktbOaYWb2BVqPSPNX5ZVuTKzMLKl31IO3pGj4M4kOzgaPZqyHRJU5r8POJhxKujOeRRv6fARZpuiuew7MIcIeSUeVtoipw5fcmDWTmnquwlJeCAr7Dg3PS8MTMquVcKH0BKxyBuI12C17K1neFiFnMTgfTO0NwUJjDSQoEjURbyUkJVsC0TVW6C0cjRtiaNlUGRx3Skjuw82dDcw2jUG/OPs4SLQhbqkunUKHr5wxVF5WNd46lzpEeqruwDDT9AnY7EpqVdIREkOXEQZY9wRISqCGyEp25Bj7NY+sJK9IYccsd9IWyQKQp0JG+0CeNZnM2kA6gRfKd5/hAhi2YC1lN9k7xW4o7LTFWGGtvUBClNaqFVmXUs6NpOhi4ThKoOHvrIi8wFIASylrTub3tBS9MtMHv2BEQ6fD43sHdUuave1x1sEDs4FmleJxV+cOnAD1v0ioNPpZlFgkgiJDc+2+BksTEv8ApMbW3LFDGKvc7KH6rPnOH6wPrHVfOBypp+jmXmrkhFxeNbqL2SXWrkAYxXFc5ZD5v41HWKXFYI4w1rBurfDZpHlxcbqywrVy2QkHeNDkJwuNg3tcRjmHJgIKVHlvBQccinJ/RLUlO5tE6OaOKJocVWVUtnlyOZ+rtSgtMOdnfYmIaKgh9xKkLCr7Ec4Aalj+Qr0suXfQUOFPdVAtRsZOUedLU04pTSTcEnYQy+vjc4AHRQ46xgdYreWnhoSbQ2/WJdg/XPNp9TGS1ziknsMkgu1x4r7RWYZYnMWzZdmn3Ayk6m+8I7ELyiKNtygfVAvytRIf0VzFLVHLoI6RcrXZog8tooakq4VeNQ9RWbBV1KN5hYgnk3MoAI3gYpX85V5wTSuiBFTKe+VaQjuBE1JGoj502m1eZjlIOwjrw/OzfrFXKdSrFg0RFR1q0G4jW+Hn9LbbLGQ0hWojXOG5u46L/ZiCdrpZdkeME9kQT8OkQVrtPMeTgiUgmyrajrERkhx9JVuFi0WDDo1VdtStYafzMN2OyBDK13VvrFbLzhSygG/hEOmYKhoN49ohhIaPReUTTgvPqnlODMQD8Y+Z767XiMpaukJDpTsIk8M20Ubii+qumVIToflDE4lt4EG14rTMq21hBeVrqbmBbAQb3SvqWkWsnlSqUm9x6w3XQBSyP/TP7oRnURreHK1c0s3/ANmf3RDxW/BFypmEkGY2CwKZSRMOkfeMajgoZqI16Rmsyn84dA++Y1zh5LIVQmSoC5vFPgDstQ4+X6hX/aJmelaPMKcWlW0STHzEqp5wAjSCBUugDS3nFbMzTcmq+luYjVPrAxpJ0WNFL3gp0sylpFgNuUS21aabRQN4jlybFQv6xZSdSafHdUNeV4rTURSbOBVhG3LpZWQUOhjijdOm3OPkFJTcG8cJvt847ROglOMgBtVusBeKxlmh1g1lxdtV+sB2KU/noB10igxX7yvcL2CDlnJUWieu0FlVlfzBt1I2TApM/wA/a/tCNAcYDtIQLX7sUJGaNwV4Tle0rLK6vQDzijrCQqWT6QQYllyw5Y7XimqaM0kk25RkK3x6rS0nhQ24LMKgUrQ7wgveRdlQGhgQrWiwOcLEeafcFGZTdOscNgswqX0TeGln6w2jQYeNVVVnhUltQJh9JsdN4iNmxiSjeLdqrCpCNtYWDY6Q0g3hYPSDCAp29oUFX3hvNeFDWCCAjVZjxsCpYYaqKDb3KrtXPQFUa7VXwaitbeoebQsW53SIzDjdKGYwHNOJF1SzqHR5WMG8hNe80WjzBNy9T2yTffQQvMr0fsg7NS5ehI97FUHGJj6Q4c1JB1LNliMW4JcUzgGqLlakpSqPPd51I/oVfeEbljFv37C1WYFzeXJ8hpHmPB2FP5Vy9Ql5dzsp2TOZBOyhfYwoF1pq6SOmjzyeHmtZ4w8babiLD7tIwvMreXMfpXCm1kxn/s8VgUfifKpVo1UWFsKHU20iK3wvrj7gafMuy0fEtA1I8oerVORw1xLhqdkkKUJZwOOqO7lt4cDSRdU8mM0L546eF1yV6rKikFB0KCU/KMK9qWQU7RqRUEjRl0tLPTNpBRNe0BhVSS4hyYuvvFsDwk7xkvFvi61jqmJplLllNyaXA4txzxKUDpaEGqvKgNNO655LcOC9QVUOFGGnFqJ7JtxrN0suDOeZE1Tp5lY7rsq4hXQC0eROHPFyuYNpCpGnOMvSiHSpLDmuQ+UEda42YjxDKOSqnW5Rl0FK+w0JB5XgCgprOp2m/JE3s3vmWxhUpO/ddZUbdSHDHqLPlShKb6gaiPKHs8suPY/VMy4KmJOUIeUNrk7X6x6ylAA2kqFzYfCLqhNotV4h22Y0Ys63QIb4rS5f4YYmQfEqQNh03jO+FuNKTVOH1ClDUWUTlOlg06w6q2W3MRsdZlWqrSpuUmU52ZllTTieoItHhvGmBKtgWtTEt7tMvSWc+7TTAPeRyB8xEava4kOAWg/49rYIzJBI6xJBHtZekMU40o1Ipk25NVBhRDKkhCFXKiRHn7hLRmsRYmrspMNhbU4yp1KYCnZeoTKhaVnXDyzJJjbPZywhUKdVJ2tVtky/at9lLNK3I5kxHhYSQSth2pxKFlA9od3uWut1RTHBRv3kJS/MobJ/Rjl5QQY9wUjD3Cx1qWZLTbDqV3O99NTHoFiVZUoKU02pQ5kRBxjRGK9RJiSm2wtiYQULTbl5RJfGyxyheYR45WGWJ00hcGEFYrSvaBppoVOlqlIrE3KMBtTg2UBzioxbx6TUKTM0+jSgDk2js+1X9kHeBXEPB+q0mbcZpjqJmVCj2ecWUkdDFdJ8M6y44EuFppJ0PMxAELzpZesM7UYYIswlA97oYo9RnMPzImaY+6w8w5mD6NkHofKNpoXtOTrUqlnE9NTMupFveGftQ/hLhvK0qgzUhONpmxPG7yljW/l0gKrPBlcrMuGmzMw23fQFNx6CHDTP0KqaPtjh7pHxvBAB0Nt/2RPW/aVmFtKFCpgQ4Ro47ygJwxxxr1DxFOVGoOfSnv6Mrss4ruo/s9I+leDk7OKyvTTxRzKU2tFu5wdkWpAsZXg8dRMHxQvBcOSlTdsKBjrNcSPIKzmfaTqz0upunUtiVWpJSFnW0ZOZqbXVpiekg8qZJLry2D3kE7mDNng0EnK5OTS/7IMadgbhQxRac6lltSnJkWW44LlQ6QIjN9lHrO2VIyPNDcu9LLOcPce8RUxlLUwWapLosLOCywOkFMr7Slv0lEUlfkYRingvLKfW4zLql1E3u1oCYD1cK1tOWE2+AD92FEDk/T9tcOkYDICD6X/JEGKOOlexBKLlqfkpUs4LLKPGR6wF4Yw+9iistWQtyTl19o84r7Z6X5wWUnhbKZ0qnnZh632bWBjS6JRZeQlksyrCWG0jQAWg2wOBuVXYt20p307oaQannsvP+MMPTGEKwp1gOJk3l52XEXu0fXlBfhXj/WqNLIl6uy3WJZAshaj3wPMxomIKK1UWFtPNJcQrxJWIzWr8KZZxZXJLclVHUAaiEdFromsG7XMjiEVWDpzCK1+0nLZB2NIcz22JgTxNxsreJWFyzGSnSixZQbNiR0vFIvhdPJVYz6MvPuxeULhdKNuoXUHHpwg6J2TeAETuavZ+1+GNZdhufIfuqChYRma5T5udbzNJbTeWuP0qhv8AOL/h5xcrOAHSyyDMSea0xJO/YPPLGmy9LbYlktIQlCUCwSkaCA/E+AJarOqebBYf/wBogb+sG6n07u6z2H9sv+pe2qZ9m7byWk0/2icNVFhJmkzMk5bvNlNxFbiHj/h6UaJpqZmedt3U2sAYxtfD6sMEhstPDr1hr8n9YmDlcDTCeZMM5H3tZa5mNYU1mZsw91E4gcV6xjVwN1B33aQbN0SrZsD69TBXwfws4y8mt1VrIs6SjSxqkH7RhvDnC6VkZhMzULzjyDdOcd0H0jRpNktgAchpEyKLLqVh+0Par4mI01Me6dz18gs/lcVvcM+Ks3NoSVsuqCnW/wDaNne0ehKTjOk4nk0TFGnWnErF+zUoBSfIxiHEjBZxK01NyR7OoSw7qraLHQxkocqdBmCl5M1JOg+JBNjEWSItK03ZvFqeppmMLrPaLEL2g5NlCblTdv6yxYRgftE4ulqm7TqVIPomBKErmVoN0gnlGZuYoqc0jIuoTziTyCzrEigYQn8T1BoOsPNyKVBT7i91joPWADSSAr7EayCnp3Pe6wAW+cBeITFWw2zQJ95KJ+nA9gFG3bNk308xGkuTpl3szaihadr8o8eYkoc9girEtreZYC88nNN3GUfdJgkpXHbEkmwluZ92qCUjRa/FCZXDQpaCpp54mysN2lesafiGcTm90TKSzihYvssBKyPW0cWkrJU4StSjcqJuVHrHk+q8bsSVZhcuytqnsuCyi14resF+DPaBdpFHYkcRSrk57sAlp9B7xHnHG/NPBkYeeE2y1niTRZGuYFqsvVphcu2w126FI++NgfKM64Dzcu9hV1hs/nDTxU6nyJ0gc4h8cFYnor1Koko5KNTdhMPOK1UkHYRa+z7KLRSqhNLBCHnAhBPPLvD9KSJQsr2yjaMLdm3uCFsTau6B1iW2dBeITW4ESkLA05RcheNlSEnS4hwK2B2iOldiIdC7HyjiuCdSesOJPM7QyFD5w4jYCBSpxOpMKOpsLeccT5Q802FOJChcX1HXygSQBcogLmwU2k0p6bcS7bs2WzfOr+EW87NjKUhxTihp5CEVGebpdNC5tRT3bhCeY5ARQoqTj0sXZhIbz6pbB2TyJjxXtLjcldOWjRo2XpmCYS2mizcyoVdqCGEKE4+VKsbNo5esQMPPNrn21LB7d6/ZI5No5qiorM/LomCVlTlzqBqVnp6RYU99MlLLeWQmYfTqf9mjpGSaDe61uQNZZTceYoao9GmHyvwJKGrbq848H8VMQu1SsuBRytINw3989THovi7jVtmcZlFG6UJzqSD4RbQfGPGuOMQqfm56aUe8tZCLH8I1GBUb3y5yN1R4lOyKLLdVGG8NuY9x3LSISXZdt0Kd/ra7R+l3CPA7OG6DLoZZDSUoF02202jyr7H3Dzt5oVKbbClr75URfLHuXthT5I9ggLWhByIOyjyvDuPVfHnELT3WafNRsNg4UWcjvP1+SrsYYzpOBaQ7UMRTSJWWQO6gmynD0Ajybj72tqrWZtxnDpTTpBKvq8vjWOsH+NeD0zxDqz1R4hVmYedUohmUYUQ0wnkAIz2o+y9hvOTLz0635kkw1QS4bFrOSXemgR1MFc//AAgAeuqDqfx5xGmaTMNVd4utd4FZ0NuUeu8GYscxZhKmVaYR2bk2yC4kfe5kR56wj7MdIlaiXKrOOzcuNW0bW9Y9CU2SbpFLYlJNAbYl0ZW0D7MMYvVUchaKcbc7WT9BT1LLmYrW+G9RSUPy6iCb5gOsGzz90d06D8YyDhq44qrZrmyE667xqLjxUY0eAzF1GAeSzmNQhtUSOa4twl9Ouw36Q1NzgbSSrcbDrDbswGmVunUqNgIDMa4iNLpysqvrXdBrtFhV1TYInPPJRaWmM0gYFV42xBLE9mlSn5nkRsiAd+eQ2i7veX9hI5nzitnZ1azdSznWb2vt5wHY5x/J4MpapifXZOwAPeWfKPPZppaybQanktvDDHSxWvoEWzFQSgKU4oFRPeAP4RDeqLjrRAvl+7sYwWR9pSSdnSl+ULLV7Jeve3mY0qm4xZr8kiYk32nWl/abP746ow2pprcVtkUFbBMfs3XV5MTDwbVZWRXInUHyjMscTbzku8FthDqRdWXYiDR6eyJuAtTZ3SNcvnA7XWBPSa/tt20UN/Qw3BZjwSnpQXNK851HFMwiZUzLZ0NAkqVbUmND4cY2mZeYbbecV2a1WuSdIy3GsnMUjEikMLyharpvsYIsMzZQ632yUoeJ0I8LkbKrpopKYEDcLOU08jKggnZe4sC4medk2kKUXW1bIUdSLbgwbqUH2A7LqUtu2yjdSfWMA4XV/wB4YRLuLKf9konVtfT0jVZetPJGdRKXEWS6BssRkqCudh9Tm5c/RXVdRtq4sux/VEOe/pCh1iJLvB9IWg3SrXSJjYvHrMMrZGB7TcFeeyMcxxa4ahLTpHdFDyhKjlB0vDKlOlPcB9YJ+iRounNBre4iJVHfqQI4ovp2EV1QDyyCu4ER5HaJ+Nut1JlnLtgCJIWANdIiyiClAvD6hmGkON2QO3SlOW9IYWo7nWFHbWGH15RrvBE6IQNU0++EjzgKxHMdo855mxgmm3gN/wD4gMqi+1mAAfEv5xS4ue41vUq3wsd5zugWhYQZ7CjoJG4vEeqOh15VuUWlIb7GjNjayNYrHWUuE2UNT8ot6Asj1d0VLiLJZAGsG5Vf2l7ZTqOsSpCc7OYSOsRJ9TMuAcwBB3hmmTbb04kJVmNxcQdPVucHtkKZrKERPjdGLHmiKsLy011RP2bRhmNJgNs5QdVL0jZsWTIl6UoXtmjAcYzQcmWgTsdrxl8U79Q1oWsoO7TOcpdEfKOzv8usGcxMybsqA8htIKdQdDADITIQju2KraQxOyM9VlG76kJG2sHnla7K1l1TTSkcrpGKpFqnLMzIuJU39pIO0CFQqCptF2cyl9REitUupUgKU4tbzJGoJvFJTK03IuLWtIWkdeRiqq4XZ/DZU0rru6J0TLq5lmXXcX8UHEljBVPZTI0a5cIsoiMxmMQiYnHJggINrJtFph4VKeJckGyhJ3WRvDkEEgPdFigY45rBby5ctxTT4JSbxcr0binqB7qrRtnqYw7KvpSbTCrwTSp7loGaYbzJAgkk98pipmHfVpD4QiOknvC0LmD+dmG6SMqwIVN399NuUVU+hVlEbhEFIOgjWuHI+sczaXTGSUXYRrPD25cWAdbXiE/RqKTmj7VCFW6RDkk9o/554mOrCGFHyiLR05niTyNzE+I95qrD4XFH0s0A0i+4SI5MTaWUnbTeKpdXs0EpOg0vA7Wq44RkSSD1j0XEu0scEI4errLy5lD9o4u6osYrjC15VqETmpph/RtSSYxmcxAlhf6S5v11ixwziZ1+eSltZVtziFhvaqZ8jWTN35pJqaOxyrWVIBG0cDY5iPpBXvLSVEi5GsWDTAA0F43XFFlXiO6hJaJPhhFc7tPUP6hi07KxirxF/M12+4YrMSfeIKzw1mWVYVNECZdPMqMbNw+bT/Jxg/OMUm3LTLo375jX8E1BDGGmQogEAxSYVIGyuJPJX+NN+wb6q2rdZEoClJBIjPcQYoccWUMqteCarqankKyqFze2sZdiRwyT60r5b2jP9pq2rJDWu7h6KgjaAFGm63MtO5kOqtfXWDfCldfckUrdWSrlGH1KtuuTJykgA6ekaFgqrqNKAWkkcjFTTcaJgdfVHEQ55WuUzE+ZWV1VhtBJJ1FEwBqL8rRizlTUhRUkkK6QQYXr77r3Zm5sY1eF4zIS2KUX80skQ3C1uWVdpWvOA/FN/fBbpBNSHu2lSTvA1ifSaF4lYqbkq1wsaBB81f35u33hGkNC1KQf6sZzNH8/a5d4Ro6LfRaLfdimj8LlczHVqzDGNlOG2msU042FU1F+kXeMAA4T5xVOpBp6b9IxuIeMrTUZ7oQtNN5WFGAmuaquOsH08kdiu8AVd/SEecDCfs1KPjURtV2wIaUbOECOtKsj0hparvExoqA975Kqq/CpCDaJaTcRCQqxiUk7RcBVZUhKiLQsKvtDIOnnC0G8OBAU8NoUDf0hpJvCrkDQwt11lSY5kjU8K1OVbR2i3ZdWVP3rDaMRwpx1qdAorNKqUo3PJkLttKVopqx8Jj0DNILiND8ekZPjTgzIV2oLnpUrkZl79L2Y7rh626wYGZaPs5jUWHvc2Yd13PoUC4p4z1nEEuuWzIkpVzRSG9CodLxJ4EhyYxdOuNIJlvdyl1f2QrpF9S+AkglxKp16YmLbjYGNMw3hGTw1J9hTZdEu1uQBqo+ZhwNyhWWO9pqWrp3QRXN/KwU6Xp8ulAOQZrbnlALxLwY3iunGXv2TzZzMOj7Cv8I0UoKfKIU7LB1N7XMIxxvqsUxzo3B8ZsRqvLDvDbEDMyppUkHCDbOnZXnFrTeC1YqSkiedblWVEZgjVVo9Fysiwpu7vIxPl2ZVghSUJzeYh4MareXtTXvZl0HnZYtVOArK5aWXJhcsplsIUUf0g6nzh+i8CZVbiROvzCxzAjbxMoUmwtaOsuIbJIsP4xIZHGd1THHMSjZlZKQEzgzCtNwfIJlqTLJYRfMo7qWrqTBcaslIsVAfGBh19az3CAnpeGM7nMn5xPZwwLArOTmaV5e+5J5oybqiFI0IgYxFTW5zNaykKNykgEXiOh11OxsfWHO1cJ7yvhBDLe90yWPtaypP5KS6Dfs0ZufdEXNMlm6cnQ6/uj5TilG6jDDxVbu30jn2cLBHFmBu7VXMvULrABsInvTaVMa7dIFGVrChvEhyadULaw0YddE62S4SZ+Wl5hxRUhJJ3MQEUqVDibI76zZKQm6leQAhNTnVSUo48U37ICwJ3J2jfsA4bpWBqLT55TDNUxDUWA8qaeTmRLg7JSNoz2P9oKbCowHnvEXHptf32H5K1w3D3zjiEHLe2nM2v8rDc/RAeHuC+IKzLduuUZpEiRft51VlkdQIvGeB2H2T/lvEUxNL5pYbsIP5uozFWX2lQeW8rkknujyAiGpOXwgD0EeT4l2wrag9w932+gt+ZWrpaTg7WafIA/U3/RDI4Q4GyZEz1UbNrZgkRQ1bgK3NBS8H4hZml/ZlZ5GW/leNCF76mOKbSvxpBtztrFRH2irYjmabHyLh+p/JTCxx3df1DSPyH5rz6/QHsO1f6OxPTl0qeB7gcH1bv9hW0XrLSWQMoA8o13EdKl8YUdVKxAPeWbfUPq/SS6uRSreMOlXpmk1ieoFaVmnqfctubdu1yV6x6L2Z7V/HP+Hm0dyPX+VTYjhYMTp4hbLuOVuo526jkpNTU2XpeXKA49NuBtlHVR0EH7/s4SslKsqxViBqUnFpClyzDd8gPKM1k3xMcScIME3SqdSoiPROJ3FP4hqC194l4pBPIC1hELtbjs1NI+NhOmUDUgagkk21PIDVPUFJlhhe3QuDiTYE6GwAvcDms5b4HYPaNl12oKPVLf8A0iT+RPB6kd2vVNJ5fVwUAW6R0OERhG9oa7m76u/+StXQk/ePs3/4oPc9n2hzaT9G4rW2vkJlvQwLYj9nXEci0p+me4Vtgf7ssBdvQxrSlhabKSlXqIQ28uXWFSrjjC0m4U2q0TqbtZWQuBzH3P5G4TLqQu3sfVo/NtivKE9RhJzapadl3ZSabPeZfRlUPnvCWpZLQskWPMWj1biOj0jHtP8Ac8bSqFqAs1U2EZXmFcibbiPPeOMET/D2tpptbUl9mZBVT59H6OZRy1+95R6PgXayCvdw36O/P9j9DyVPVYYQC6IajUjfTqDzA58x6aocI0tCMovqLmJJbtuLRHcFlWGlucbNuqoXFJWygjQesRSygrIteJSlWERVH6zSHAE04JwIGw0h5pPlCUWsLxIQAfKDshuklsLFiNDECboEtN3DraFX5KAMWlukd3tHWShxBuFRMYPkWVXblmAeuTaLmVpyJa1gBbYAWESEkX2t5w6BcRwAGyV0j3eIkqtqlHl6pLqZnWUPNK3SsXjOatwXkFvFdPW9LhR8IOkatvCgATqIF0bXbhTKPEKqkP2Ly1ZBTeDMs1MBU489MJB1Re14ZqPCaeZmVmjvJ93Ue605uiNpbZHIDyh8MIV4kgwBgYRZWEXabFIpc4lv67LEKZwbqM8+kVOZSywT3sm6h0jbcN0iXoNOYkpFAbZYTZI/iYkIQhA7qR8oeaIvrvCxxNZso2J41W4hb4h9wOWwU9CokNqA21v1iElVzrEhColKlspSbC1odBtEdBvqYeGschToJJ9YczWENJNo6VW3veOSp9Ll+cWFLIVNoLnhbGY+cVVwACYWubLUu7ZVs6bE9BzipxuYw4fK9u9lYYVEJa2Nh6qvxbidoTAennCWkOZUNjdauQERajVXHG0MpNn3U3cSn7I5CAKq18O1n3xtvt0yyi3JIO2bmoxb0ypIUzYrK3X7Fx6+xJ2EeEzxO3K9jhygW6KwSGmSp1/vJb8Z6nkkQPYmxoikS7jr6go2upP3jySIj43xYzT0olpId9OiQDueZMZXX6u4+O1me8hFwhB11t4jEmiw90pDnbJupqwwW5oSx3iB+cTOT80SHpi4Sk/ZSeUee692k9VZCTb/AKV4E+l41ziDUxZtCT3iMy4zrB8n9N8Q5RkDMmXIBvG4oAII3SW0aCVk628zgzqQveHs04aTh3B7Mw5cPPjnyjTa3Xw0lQCrC5F/OBnDRRScMSrTfdSloA+WkebvaK9p5vBgcpeEXG5ytOXSVhV0S6ep84w1NT1WI1Zjgbck/Ieq0M89PRQcSY2A/wB0W9V3FoZBSkKVfntrAivFh7X68qQCdTa4PxjxfSHuNHFeWXM0UVZ+RSSXJyxZYT/eMOImOK3Dp9DsxOS9UbQbuSomg/mHQxqR2Lma3vTNLumv5qgHa6EnSJ2Xr/C9/wCGJtM0lJSQrOL3GxgvDBU0M2t48+ezTxSkuIsqrsUqlZyVVknJJZ7zKuo8o9IKAS3c7c4x1VSvpp3wyCxC1UNUyoibLGbgq94cSanKutwEgNp7w6wfzM12JWTyGkC+BpYIBdaOqhy5iLuorurfcxtMFZw6MeayWKuz1RUSYm1dmhKjrqbdIx3HmIfe6o6lJuhnugecabVZnsmXVk2AQbnpHnqtzxdnXiDe7hIPWK3tBM7I1g5qwwOFuZzzyS5mdygqzd5W5PIdIxHjzRp3E1PbXTlFS5fUNX5RpNVqPu7JVfXp184CKhOrmFntDvtcxQ4fI+CYSt3Cu6uNs0ZjdsV5UmVT8g8pp6WfzJJunsyYKeG/EyYwhWUdopa5B5WWZl1cvMCN3l5GWedKnWGFKP2lIBvFXiDg5RsUIU6y0JGZI7jjYsCfONg7tDSztMVRHYHc7rNtwKeFwkgfqOS0qkVFupSrMzIuB1p9AUhY2I6QmeCFpWpk9k9bvIOyhGfcLl1HCU1MYXr6FFbP1kk99lxPOxg9nlNzDCsysqvsq8+kZCqh4U5a03HI9QtJBLxIgSLHmPNefeM7amHG5llHgd108PlCcOITOyDLitMw/VPlEvi2+XJSZafT37HXqOvrCeGrDc1hdjtj3+9lV5Rq2G2HNJ5GyoSP+tIHMLWOHk48y8lBOVxNgf6x6/KNpTVFLkkupvmSBm8x0jCcHPFiayOnvo7p8xyMarTqklTJQ4fEMqvXlGNxEfa3WkpRdi0HC1WupKFLBYcGh+5B83TSQCVgg6gp2IjBKRU3ZKbCWzZxJzJQdnI13BuKmJxkS7quzC/AFnZXMXixwbHn0j+DIe4dvL+FXYthLZ28Vg7w380SpkG076x1UsgDbSJCwUnvAi+3Qw2pV9I30c/EAcDcLIuiyaWsmjKtkaj0imrjSEgBIsb6xeKWAnXaKOsr1Tz1h0XNkJsAojbdkDpCjblyjqVgN6C8N5r9ImAKKSkua62iBNKSL3iatVgYrZxdwY4rgqifcypV0AgVbJfqrSN7rgiqayGFnygdoifeK+0BrlNzFHiXeqI2q7oNKdzlrgU3KUkFzQJR84z2bqDrE4paFKDS1aCDupN9pS+ZygaQDVhhxxsqabVYeXOArZX8RoChskMZuFU1x9yaADSiVHpyi1wTSVyzvaTJKnOV+UR6RIOru48ysq+yCILaBJqbQpbyCk8rwsWZ0gXT1YlI0VJj+ZswlCTvvGB4oezVRKQdBG38QVWUATtvHn2tzZdrLhVslVojSnPW26KzZ3aK/VW8q7ly8oIqeVTJQhs99VgIF5dYUlJGt4IaC4fe2QN8wi1By6hVOXMdUTTOBpielCl0tnOOcZxVeBk327ixNoQhRJyiN8YkXHJdKs+hGsUtclSgEKWc3WIc2Z+pTvwkLtxdedZ3hOuQfClTedKTqII6apyltpbQEBKRaw5wUVeSutRJKrb3igMkguXUoxHEr2o20cLTdoWhuXyG3KKmoapOmvOLdfgMVE+SUmNM/dU0ewVZTQUzdxtzgklDlULQO0wHt1QRyAvv8IqpvGrOC2VEdLNnQYVOH87humAlYEOToyzZioqd1ZQ7K+oyirKLdI1vh+QHiBvb5xkdFNrX8o1rAJCX79RoYgy+EeqN3NHc2vLLm5hujglKz5G0NVNz6oAaQ5Rj9Qu3NJiYw95Vjh9mVQzOIvd5h1tS72UQBeITtaamwpClgrA6wOVqnuuT8wpp2xKzaKJcnUJdxS2VZlC9/OHzEJdC6y86klc2Q3HMruI6mhqaV2Sznv1h7CFdflp5LjYUrUXtAhPl8zR96BDhVzjT8IS8hJ0xtxwozEAlR/dGjoaDNlAda3NVrn94lGlN4hdg82laikHQgxrtDqSKjJIdQR3hrHneVk2q5V7ywytoNtOcbnhKUNPp6EG+2kbDDG1Hf4huORTecXsESq11imxF/NHP7BicucbbVZxdjzEV1acS/KulJuMkHiD25Mt9VY0IPEusEmQTOO/2jBVT6yZWkIaBtpr5QOzLdpt7kc5/fE5NNmH5HtGR3enWMXWMldE5sW5WhxQjgAlJnMVmTJBcsDzJ3gWruImZ1Ksyrq9djEHFLa2HMswSnp5QHz822wDZRB633jOR00ziGPuss+W2ybnZzs5q9wQVRqmFZ1gUhCmiNtR5xhk7UFzCxlFgDGl4LeKqUnXaNIaI5WrqV3eIR03MIdUS6rQcolS+ImaSrOgjT8YFVvlKVXJuPxgdnqg886UpuQDawiwp6cR94KWTyXpnhrif+UEq+vQBBtD2J/52OekBPs/KdEhN9skgZucGmJrmbHpBzvzRXvfVWuHBCc0bT7fkoRpTac1JQf6kZrMi863cfaEaU0P8jot9yIUfhcrSYatWX4wFnN+cVzutPTbprFjjAWcPM31ivOtOTfe0Y2v8RWmpPCEOz4swu28Z/XD9YTGgT4PYrt+MZ5XFDtCBtDcI+zUv76hoP1d4acNnPhC2T3dYbd/SadI0VB4/kqmr8KeRc7aRJbOmsRG1chrElrzi6CqypANjrHQcx1hAFheFJNuUGhToNtocBuIaGsLBsNdhHJFw+cIWARYi4hd76Q27dIuIVcF1FtrDSFrULaRELuXe+uwtcx92xvYhQPRSbGCBSFqeKdIQ40FJIItC0qum8LI015wSG6pnZNaVkoJt0hsyz9u6TpF0oC8INgNt4MPIQmMFU2SZRteE55oHnFzlHpCVZQNhBcVCIlUl+aA5xz3qaHWLQlIGwhBt0EdnS8I9VWGoTSdwY6KjMjdJixugi+UWjhCCL5RrHcRDwj1UIVR4akGFCrubERIKUC90iEFls/ZGsFxEnBKQmsqG4hf00ecJMu30hPurR+zHZyUnDtyVPi6sBVAmr3ukpVp0jbuCuL0YswazKOOBc7TEgpSTqpFox2p05h+UdacSClxJCoDsGYkqfD6vhMm/2bjKj2KlHuuJP2THn/brC3VELalmpbofRa7s5I2aKSjJsSczfXn7hezJmpS1Mly/PzDUuykXzuG0AlX46YfkHFIlW5mdKd1JFkmPPONuJU/Uphb9WmVzT7hPZyzau6n4CACbmMSVhRLYcZQdkt6BI8488wzAq6vvkFmjmVp30FHStBqnXceQ0XsnCfGmgYqqSKelLsjNvGzQd8Kz0vB/YpWUqFlJ0IjwXwrp+IanxQotFlFuTcy++lawnvdggHVRPKPec68lVQeS0rMhuyM33iALmGsTwyWgflkN9rKurWU4e3gbEEkdNf1Xxvy3jCPaBdFExZRKszo4pGRwjmnneN2ScwAjzb7T1bbdrUtJNKClyTN3LcieUM4fM+OqiczfMPzTmGwiSVzSNC11/SyXQJkPcWcHrSbpVNBQ9DHp+vWVWp8/+uf4R5HwLNdrxIwaVnVLjZ/AR6zrSx9Mz3/GP8I1nbZwNc/1b+RVRAzLBTjo13/uUJaYE8TcRaDhOoCSrM0pqZKAvIkXsDBWtV0+ceVPadnHJHH6ltAEmUbGo30jF09OaiURg7q5w2njmmyybWJW+UjiRh2uuhmn1NkuqNkocNrwUI133/fH5/MYhfl3UrmGFtgG4W3oUx6Y4B8UnK6tFFq8x290XlHlnvafZMSK3C56O2cFS6nD4+GZKd1wN1t6EW1G3SK/FWGJXGuGX6FUtl3cp7x3lnwNLHoYtLZQQdxyhDi7J00O4MRYp3ROzNNiqQXuHN3Gy8qtl5p2akakgtz9PcLT6TzI5wy9odPlBZxwp30Nj+WqbaQlmsMgOEbFcCDhuo66iPoLsriv9Rw5krj3hofULKY7Rtp6nMwWa8XH6j5FNrF035xGIPaxJWqwiMT9YCDpGnGyo3HVSU6ACJCFC3nEZBzAWh5OkEm09yvzjqdYQFXhYhVwKdQbQoX5Q1mhxNyYSyJOgXEONp11hCdIWk6x1l108gXML1TCUnWOKXCLhqnAdBDrR19YiJcC1gIOvmCIltIUk2Ukg9OkJdOEaKW3qdYkoAHpEdu9rQ+lPzhy6YUhBAEOpXfbaIqdDptEhNjHXXWTw/8AflHRvrCM8KBufKFuksuk84q8SzRlKJNuj7KLRaHpDFQkUz8i/LugKDiCAPOIGKQGekkjHMFTMOnEFVHIeRCw52dSU3KilCUErt9kf9YeoNUf9ycmXUlOZRTLoHIdYpaiypFSXJLvcPFCrcwDtBGuXLDSWmcuZKQAvkgR5NHA12jgvUHykagoJxLNOh1b80o5l3BV08hAHimtqYpk2snvJAAF9hBNi+etUnVk3alwQlPIq6xkeNqpdr3cK8V1OHz6RcU8DdAFVVE5sbqoxTWUzs40q4y9iM3yiXwCkRUMWuzix3XJgBJ8gYAqzNKDClIJzuAIbEbn7PGG3JIy63GVKAIKhbcmHcTIgoH9XaJigLpatp6arfeMWO2sC8LajO5w3MdgWZZJ+2ojf5axiHsn+zFSMWYbr3Gfj+uYl+HmGAuY7FRs5V3xrkTfcE2Ag39pKhuYpomE8PpBvVqy0xm6AkX/AAvG0e3k0xwx4GcKOGuG0JlqU7OBUy02LBwMAEZut7mC7JNjp6B83NxN/QKH2k4k9ZHANv3TNQ4AVv2kuAs7jys4sa4dYKYpz03h3CtEIabYYbBye9KG61BO3nH5ZO1uUlHO1kX59iYSsguJdJvY76mDbEHF/FzUvWsO0vEtWlMOzj5DtLbeIaWBy30HlGaOy+Yd3bpG1hYzKDa1x/t1k5pHhzmg3sV6L9kXEkxO8YKa/KECdmGy1PoGgmWhsu33rx+iD5v3UkkA2HnH5r+x/S5hXGXCTkpmBLrgcI5pyx+olCw+urVctISezbWSpXIR5h2whz4qxke5A/Mr0DsvPlw4uk5EowwtLCSpKSdCpN/+kOTSipd1aE7RdppiZZhCEDQ6BP8AGKuqISl/uG4bGvnF3BCYoms6KtllEkhd1QVjFSk0p8k2sk3t+6PPFSc+uWfs3JPlHoLHSVu0d9LRtpqesed64ezC0jQnT0jKY9/nb6LTYLbgO9UI1+pKLhzHw7QEVHEKJclRWhI5rcVYQrilitjCVEenZrvnwstg6uL/AMI8vVjFf0/OKfxJNTMwpWolZVeVDQ6ecWOB4E+tZxDo38/RQcXxplG7INXH/dV6SlscygdShNRkVEnbN/GNDolbTMMpVdPe2UDdJ+MeM6TKYXrDyJZc5O0GZc0bemO82TyueUF7FZxZwPrLDFdSuYp0ykLbuvOzMt/eaX/CLOt7JhzDwHd7oRa/oVCpe0/e+2b3eoN7eq9XuqQ6pKiElSPCojVPxiBUHSGlpXcX0J6HrFRhXFEtimky8/THAtiYSCDfVJ5pMWE7MANKFr3FlA/vjEujdG/I8WIWqEjXszNNwVjPE97tZJ9Dp+saJ18ofwUhUlhKmKbvmAK7dREDiJZ1brZOqjb433gikQml0KTl2gFP9hYD7otvGqDrUbGdSqENPxTn+SKKBMB6opUyrurTYnoekHUhOqaburcWBB5kRl2F3PdZ9tIVe5zHz0jQZRQdSsFWrllA9IzWJxBr1fUTyWIhYfEyoALyLBzMr6eUENLrS2lDt7JOyiOR6wCsOuNjKs2A59D1i0k54PA9roR3V+XnFO6MFWActuwzjl9aUy6lpmUjdtR7yR5QYomA+2lxAKQoXsd0x5qkq0ZOfCc5SUHurBsQevpG04LxV9OyXZThAnGBqf8AaDqI0nZqodHPwXO0O3qqHHKYOi4rRqN0VLc6mKmrLBCfWJxOotFdVTfJ6x6EG2WNzJrdItCVLttH19BDTqwN4eumgE285tFbNOXMSHl3uYrH3bruTpCDdERoq6rqAl1ecVmDEdpX1LI2h+tTbbYCXFBIPPrFPTqo1TJhTrDwzqjO1k7W1l3bBX9JCXUlhzW0gpKAk2IttHexl1C3Zot0tGUKx88Nnxp5wn8ob6f6UfOHf6lD0TRw+QrWS3Lt7IQPhEZ99tCDlteMxd4juONhKlgW6GK2b4gO8nQD6x39UhvoEP8ATZOalY7nQqYXr4AY8+Ts521TeP8AWMabiLEKZmUedccBVlN9YxtubS/Ou22zGIdMRJUuepFT3KdrEV0+ZunKd+UEtEmuzmGlK0soa9ICZN4JI1sRtF/ITRUoctrRcW0VY1y36Qqafc2zfTKLiKKtzqXFk3GnKKCkVlSpFKVLsUi2p3ivq9WFlWcAPrEEkjRTB1UOpTYWt0X0gfdmgDYHUm0NVKpoyq74v6xWSEwJyYAKvDrDFruASl1hdbAoWaHURUVKwSbRdTCcucDkYH6mq1wfhGnfus8zRoUKmKAmFecEUkbLHnA1TdZhRO0EMobkGKqbxlWUHgCKKYcrghU9rO38oj00K7VJBuIcnFXnN9Ipqu4CtYBqr6i+IeW0azgM/XW8tIyOjGyhf4xq2BFkTQHlEJ/hHqieN0aVVWVIiRQO+lY20iFVTciJlB0v5g2iVFq9V8gtGsmr1XclqzOIGqUukWiRT50PtFayL8jA9iyY7LEFQTf+mOloqDXFSqFJQruneLeTD+LHdm68tmmLJ3X2uVPxc43mStIAWD84rpWuuIZDechA5X2ilqlZVPLsTYDYxJw9S3a1MpQO60kjOYtqSMU9OBKdlCe8vkOVG+GMWrknkdmbax6BwnjP3yQSXk2IG8ZzhrBVLEkjMyFWA7x3vBG4E0dsIlUjKNgIjy9r3wsLIB7qfT0FjmeVdV/ExQsqbOsXVFnDO4f7RZuSkxmFXqSeyV2uh6dIPcIPhzCKVJ5pMRsFr56qre+U8lYRNAfYLPJwgzr3TOYsZKquS8sWwkKSPwijm3yJ94f1zDrc6EJtEyrr30ozs3VrikYNPY+SGMcy81V3gJdBAB1jOa5R5uQXd4KUk8+kbcqal1KAesQdvOKTE0nIzDCh3bjzilgxuTjAyBZh9K0tNjqsbYllvLACSTpawjX+HWGn5ynXUC2gG2ohGAcIy0y6t59CVJCu7flGvUuny9NYKWglKRvbnE6q7QfacKIa9VLoKIkcR2yA6jhN9pYb3C+cPyODZeWIU/ZS+d4J6lUkKUSm1k84Dq3iQsPBKFaDeK2pxSrqfsmut6Kdw4ozmK1fhvLtyqZhLIAA2AixxHYzQv0gZ4NVb6Vamlg3KdDBJiT+dp9I0NA17cOYH7qZTua5927IWmdZ5u+mojR2jako80Rm82fz5u2+YRoqFWpDf9mHo/C5TJd2rMsZjK4bbXiuBvTh6RPxkbuHpeK4G9OHpGNxDRxWmo/AFQVE/m67c4z2vWSo3jQJ/RhfzjO6+QFm/WG4T9mpZ8agtG6fKEOmy4+aPc0hDpGc26RoqA975KqrB3U80b7RJbOm1ohMKsdImNnSLtqqypKTcQpJyw0k2hYItBIU4Dci0Kv10hq+sLCr8/jCpEtBB0GYkmwCRcn0EdmZd1lAVMS0yyk6JLjRSFHlrGjcNEyGD8FVLHFSlUVKeEx7rTpZYulCtsxiY1xseq6TJY8pVOnqXMFIfDDQSuXudCD5Q8GMAGZ26l02G1tRG6aGO7W+dr23sOaZl5eR4aUqR7SQl6liKoM9u4qYF25Zo7ADrCp1UrxYw9UAZCVp2IaC0X23ZcZUTDQ3BEO8b6YimVmkzkksvSU9IJEqonUJA0Biu4NVWmy+K59FSdbMtM01YKSqwXYHuX6xI2fk5Ka2ChkwQzgfbDXfW99rdLLPJfIppLgzlKk3ulNwPjHzhHI3B5xqtF4uOJnJSQpWHKQ3JLe7BMoWsyym9ul7wKcXZGm0niDUJWgpS3K9khxbQOjTp8SRDWVtrg7KvrKGopHMEzbZhca390HG94+KSd46NTHQYBRU2pJAhCkHz1h1W8cVt6wtl11HUgneOFs21h8iPiL9I6yW6j9lbeOKbNtIfsLRxQ0hVyj5DzjhSbRIKNITl000hbJEyEaabRzKR6w9Yx9lBGsJZIojzZWNd4F6zhxup3Q8k5L6KGhSYLnRlHlEdWW4zDW8c5gc3K4LmSOjcHNNiFT8LOErGL8XqozMyJTsWi9NzjozKQ30SOsbUjglgiWSWy9VJxAuFkHL2nnGdcDKmGOMVRYJAL0mUovz0jdUA2sAdCbix6x452l7Q1tLWvpqd2RoPJbJsLpGsfI4m7Qfmb3UHDGHaBgVl5vBFHapzsynK9OuWW8sdM3KLNgZdtoSEQ4252SgbA26xip6yaokzzvuU82JjAco3VHjXH1OwNSHZqbeaemcpDEuhdyVcr9I8bYrr8zimsKXNLLk3UJjMs7212HlHqviHwjw9j5CnJN1+iVpQ7jpWVMuHz6Xjzk3gOawfiybksSs9lU5YfUoPgcR99J5xqezNBDU1jAXg6/kpzaqKmoZXRg57a3/AE8le4PX2PE7CCL7TCU/KPW1VezVadvv25/hHkPDpycUMGEbKnbXj1ZU3FfSs5cj9Mf4Q728flxN4H/b+RVXRNz0sBP4Xf8AuUntARa9jHlv2nGO34hMg6gsND8I9LdsQN7x5t9opYPEJi/NtmKLs/IDXx+oVpQsIe//AMrvyQfPYSC2kuS+pCRcEb6QzghbmFcYSEwyVISJhBt93XWD1plPZIt90X+UUNepiUTsi8yO8uYQmw65hHufaijiqcLlzDUC4+SxfZyukhxBjb91xsfmvYMwci0m2jqErHxERnFddzCq46WHpJoHKpEm3m9bCK/tivckx871FmSOaOS0kEZcwO6rKPaVbC8KUyZFguWmrJPlGWS6w4y2snxJBjUPaQdSMDy6FKAWqZOUHnGT0kkyDF9e4I9a/wCMZCYp28rgqt7UR2ooHdC4fqpazfeI6hdy5h9WsMWuuPV2rCFSGjYQ8DeI6TpD6NReDQp1JtrDma4hpO0PJAt5wq5dSfnDiddobtYiHE6C4GsIiTqTDiVXMNo03j4qyAqJ0G8cUO6loubbfE2g94X4NkKuKpXMT3NGoLYW62neYcPhT6RcUbA2GcH4SptY4jszVQqNb78lTpc2yN9TBDT6NR6lwvr85w+VMSLDcylU/JzRuUhMMPeNgdVZUtBK5jJ5WkRFwGbluq2j4soeK6i3ScSYfkZWTn1dnLTEuAlbH3ST8o7S+CqjMVN3FlWaolLln1NybytXJkfet0ik4WUGUxBjKXcnZ6UlZemkOpl3nAkzC+QHleHsWYim6riiorrK7uMPKQGs10NIG1uUMsc9rCXLWV2D4fVYiIKN2Vobd1tdfK/O26g414fTmDCxMKfZn6TO/wA1n2DdK/JXQwLjTQ8o1qSw3UKxwqmZdx1iTZqdTz0xE2vKDoLlIOwgGxjw8r2B+wcr7DRYmdGphleZCj0vEyNxLQSsZX0raaodE12YAkA9bf7qqFCrEX2h5Ctb9YiBdzrDzatjB2UK6loVrC+cMBfSFhel4QhddOnQ3jocsbwgr0+ENlesIdkqy3GmG003FCJu4DU2rMg/dVAzWap40NGzSNFW3UY0XicEKoyJhw2VLr7nmYwuu1pMqFFRsoJulJ5jrHm2J0rYKx7G7Xv7r0HDakzUjHO3GnshjFlRQw+6XVDKjvK8+gjGa/Orn5okfbJ+UFeL6wZljRV/eHST1sIDnG1PlSWhmee7qR5Q5SjLqUFR3jYKTgDCL+NcUNNNNKVLyygE2FwVR7v4ccNHKJTmSWkoISAO7rAP7LHB4y9PYnJlCAlQzG/iVePW7WHFty4Q2gKuLCw2jK4xWurJi1nhboFcUUDaSMZvEdSvNfGihPpplNqcgyXZjDdSan0oA1UlJGa3wvGm+2bQpLizwQwNxGof57IUSaD0ypkZsjD1gu/QptrF9iXCMyltRdl1KBGpKbgjpAfhOer3C1NRkcNMsVfDNWzioYanxdtYV4uy6XiZguJRU8T6SpJaHbHoomKUUs0sVXTAOcw6jqF+Y/GjhjVOH2I3ZxbDkzQaqfeJGoMpKm1JVrYkbHWBGgU57EM8zJUaVfn5x9QS0yw2SST16CP0hruCaf8AXM4DlZqUkppZUvDNZY7ZtpR3DarGwvBdwU9lDE+JJszFRodFwPR1EB6al0XmHk9EX2jS0uO1XCELYxI8aBwdYHoSNx52VFV4LS8Q1DpDGw6lpbqPIdfJZD7GPAOeoGMff55sPTVPl+xCUC6UvK3APkDH6KUDB7eHKcG3svvDozPL8+kXOCOFuH+HdMZk8NygQloWDi9VrVzUTzJiyqjbbNxMrl2if9q8lP8AGOhwx4mNXUkOkIt5D0TE+JxuYKenGWMe5QpUEBdkS4OUaX84oHqWt1bp2S3uTzgzEqXe80ht5P8A6LgV+4xCqFPbdBCitk9CLRK4VzdA2WwssmxfKlynvNtJurKfhHmPFTZl3nUruFBVjHruv0xyVUb99vWx6eseaOMlBVTKkmZYReUmT4vuqjI4/RucBIOS1OCVLW3Yea8T8dsQ02oY/odIrrrjdGk5ls1NbPiS2T3iPhF/7a1Q4By8thKlezMwtyblmErq9SSe66kp8J/r31MY7xZDr+Ka3MO3Km5vszfpfSM+eaKhYJSkD7otHo2CU8cNDEwch+eqwGNzyS1kjr7n+FOo9ZbSv3WqJDku4bZiNUR6Y4HyDfFjB2IOF+J/zlciwZ7Ds0s3XLkDVCTvl8o8nqZKbx7H9iqgzB4wU5TyFZpaiLcmM3JJBAvD9blZYt5/pzQYcXSBzXbD/bIL4A1KawtiSq4XrBUOzcVlQeSweXrG6VN8iXWVJFrWUYzjENKbpntNT4lG0htxoOLsNjaDfEU1klVBOgA/CPMe0TWnEQ5v3gCfVei4DmFAWu+6SB6LGsYTfaVUNg5ipwDXmbwVrUGJVCArM8pOp6C20AtRX22KmyrVDV1mLh6oKap6nXCe1mPCL8uUW3w944wOihmez3Eq0odZKKgsk9wKyo8o0un1ALls6FAgWUn0MYfKTXZNoN+8VXB84NMM4jS26mWmXMocGUX5HnFdidDnGYDZTqGtDe6VqktOpcIQ4RmULpPUQpM1lWV8wcrieogQ97WqTC0qs5Kq1tzHWJbdX7ZQUtWUrRooc4zb6UjZXbagFXM1UrTqSsggd1ducarwnqyJuZ90dVaYaGdh0HxJ5pjztM1NRqRsvc5SL6GNi4QMOTrKZ5i/aSToFh0ibRUjm1MZHVRaudvAffovQ3b3AvppFdU3wSn1hS3s1yNMwvFbUHNU6849HA0WE0XahURKS+cdIojiYuKtlFofq7RmpbLmItzjPZ6ozki+6lCUqDY3ivrXyseMp0VnRMicw3GqN5iunLqkeUVjtbJURlHnGeqxtUHswbYBANogu4tqQVpL31iEJpz95TBHAPuo+qAZqgAfChbYiK80CUtqpYPLWA9OMqo3vL/hHFY5qRBBlbxGkDnHMdVIY5gFhoipdCleRWbb6wy9RZNIuc/lrAm7jmop/wC6ExAmse1HlJmGshtsnOK0IvcpMoN+0v8A2ojKocms3Id/WgIcx9UOcmqFtcQJ8EZpMj1juE4BJxWko2OGpB1spWlZSR1iuXgiisklDKrnfWKdPECcCBeSMRJniDNWP5iqBbnB0SvDCNVfpw/S2VaMn1vE1inSDQGVog8tYA149mVKuZJYhSMeTAOsm5aHi6W2iZaIb7LRky8rltlWkeRhhykyLoOcOEHzgKTxBeO8k5f0hQ4hObe5ORGJn807aHoEUDClKWol1CzfzhcthykyaythpV/WBQ8QlgWMk4BDauIht/MnNIC0xPNF9iBsFrsw5mzHqesD1SVnUba9YtluXQbRVTpAuecbd+ixTdbKNItFtebcGL2W0GkVMuQLW0vyixlVFJ72oMUrnEm6t425W2RJSV3UOZEKnXMs8Lam0M0ZV1m/KEzzgTPnNz2isq1YQFElFcupJjVMDOfnaLc94ySiqOZJEargRz87FukQX6tTjkb1dzKoecUOIazUaLR3ZukgKU2LqB6RaVtZzpudo+lpVNQkH2HBmDjZFoVwc4ENNioco7iwpniY0xNuvVentTanlXWekW4r+FcTMH6kyaj9ofZMZtimVFLrc7KnQNumwge99XTng42T2SvEIy4xnEIHlrJDcLJyUsLyc7QtJqmFCxd2nPJmmN7pOoEFvDaQu2EuDIc3PnGR0vE0xJPpVLOqyq1yE6KjVsG1xqqpHu6w1MXGdMamh7S/Gw8CfR/5qnnwkQycSPULZJBKZRodmdhtfaK2r1oMqUUKCrX57RAmaoZCngZrrULXgErFfW1mAVcm94OnonTOKZlmyCysq3X0vKIz2ttrvG1cNj7zgRC730MeUZmdmZ57LKNuvLJ2Qm8eqeELbzPDhlM42tp3KboWLERrcJpmwyH0TVFIXSkoEnpe0+/b7xgRr1c+jJsp/q7dY0Cbk3lTbxDarZjuIyLiZIzqKlmlWlKRa9xygKmOOXuk81fYs4/C3HKyU5igZytbgCehMV1RxWmZAQyskXsDeAmZcdJPbXv0h6ht+91FpB1QVaiAGFwtbmPJY8TOcbBbbwyqUyqXKFtLDZNwTGmomLMHMdLa6wKYRdYbkW2UpSmyRbziwnVqS6qyrItqL7RipJM1UXAWWso4wyAC91X1mrIZC9QEi94zWpVxM3NLyK7t7bwQ1qYTMvONqV3TcXgArDaJCbyNK0PXlF9hUTA8k7qorpHHbZeh/ZuV2ktP2N+9GhYmTebHpGZey+/2klP2N7KjUcTIImQRzEazalCscL8LfRBk2bTrfLWNFCgaO3rumM4qBtON9c0aClVqM0T92IrHd1yuJh4Vm2LvEfWK9P8Am0ekTcVOhayB1iCFf5MA8oyGIeIrSUY7oVBPasOaxm+IV94+saJPK/N16xm+I1AFVusBT+BSz4lCYXmRCHl5XOtxCJRXdEfPquseUaDDz3wqqtuGlSEG2sS2ldYgtk2iShVhF43QqpUtKgBrbWFZxbSIwXCgq40h2yRPhd4+7SxH4w1e++8X2AabJVnG1Gka2fzGZmMrovbOeSfjCtbcgIXGwJ6Iq4aTTVewxXMIT6nGW5/85pkwps5A+Nct/OBFUy1LyczI1iULVTlFdn2wNgORziD2vcTq/Q8ZTshKS1Mp0rR3+ylpAy4sEDZRNr384areJadxVxbhuVboiJKozE2lM8+3omZQLEi3SJJaw90HUaK+a+vwuIOcBw3jNob2Nv1CTR8F1SflpJXEyq+4YVpyA5KPLXdb6Tr2bcX0ziHh/iGVdoDtO+g6cbe41NkfWocHNfO0B3GTFRr2N5qXlzlptEtKSbAPcTl0JA2vAW3MnNckG+9xeDLrGzQrXDOxrKmlbUVTyHPFwBplvr8yttwFhyRotIrVer1QprqKTdEnWJYhQQDoFqR1jN8eYQnMOzktPTM6iqyNbSZiVqSD/Ob6m/Q2ink6iuTl5pmWWpMrNoyzEsPA6nncbXjQ+KiJdrh/giXoSFqoiWlFEwo3KXbaoPSBtcHTQfuqzHMKlocrnyF+Y2FxoABt5G/LYrLknXSFA9ecIuRvprHCq8MrPBKOg03jhOmsczQkmCslXVGx8o+uFDWEklRhJEckSh5R1WgEc2EJUQRCgLkrNpCSdekJvH2YkwQQkrug0jt4RfXSPr2jki+cAKYgP6fA6RMKyTYak/jFpgPBE/xKqNQbp0yzTaVSh+d1J4d0L+6POIdbXw0cXElOn1T9PTumJtoBuTsFklYrE1grGLFZkXjLKWO4/a4SroYK2/aIryj356SUo63yxccSOGc9QpZTOIpdFRpjhuxVJMZ0f3huIzM4Llwi0qhl5PVKhGBquz1Bjc7qqKQi/JbSjxU0dOyKaJr7bHyWr4b9oqaS8hvEksxNSqiAXmD3kDraNrl59mfkmZqTcDrEygLaUOYjxHU6IaQ4FslTS1EBKArMVE7AAR7HwZh6bw1w3w3L1lKm6hMtF5TSvE2gjS4jF4/2cdh3hddqnSVdJUMbJG3I4m1uR0vf5KxW5objuq3EZ9xtov0zhIz7QH0hRj2ku6PFk+0gnpaD9Q58oH8cFH8kKwV+ESq7/KM3BNJTytkYbEEIo2NLhf8A26894TnUVLH2B5pmwC6gLpH2TbUR6nq61GrzmtvrjHjHhbMH8pOH2io5WqilQHIXEez6qL1ScPPtj/CNV2ycZalsh5hp+hTMMLYmsYNgHD/1BQy8pBjzl7Q8xm4gylzu21Hop0XGsebvaFkp+ax5Krp9Pn5tKGW7ql2SsC3mBFP2bt/U477KbEAA8/8AaQr+XUCy2b/ZH7ovMDYYOMuItDppSTLyLnv8+q2jbSOR9YhYKwNirFqWkUakPsIAAXMzw7NtrTc33tG3YVw5TeG1DmqbRpn6Sq9SUFVWqkfpCP6NH9UbR7L2m7QUtPRuiDgXOH++689w6glE+bmNvLzPS31KmVufE/VZl9HgUvK2P6o0H7ohh2wji7AQwVa6mydyegGseBTOJcXHcrcRxhrA0bBYx7StTS4xR6eCCsAunXa8BNKRlkmNfsfOI3FLEpxfjmZXLnMyy4GGbbEJ5xPl09mhCfupAj2z/jakdHTSSHnYfqs/2xfkigg56n3Sli38YaPi0h9YhjZcenBYMp1AuIdRvDKFW16w6gnlBoU8k284dSrXSI6SYcQrXSFCRSALiFpMNIUYXtCLrp4KsYS6M7akpIFxaOBf3hHSrKhSuQ105xyVu63jELbnEHhfQMQYbSZqbw0z7pUpNvVxKBsoCKXAnD+tT9MqVTxDUnsJYTmwn3kzByrmwPupi6ojszwJ4fMzDCkqxTisB7s16olWLaEjmTGbYmxnWMWPIViCfdnA34Gz3W0eiRpEKQND83Nej9n8JxDFMJ+HcQ2EnxHxWvrYbb7EowpTHCuVmVMTUtX5oJshuppUUEa7gRPrHD2TwpVZHEgmnsQ4FW4XZt1jvPM22Ssb2jNJaYVpqdOXIfCNQ4L1xSK49hudX/krEkq4xkXqEPEd02845khcbOTuNdkY8OpnVNC5wLR3gTe4526Hmh3EfEN7iVUHqsiQmW6DTvzeRlkgpbYb2BJ+8YMZqspx3wKm0sSj0grDc2m7bq8/aJt4rwFYa4jUqgSFSwFj6nqlBTpl1oLZAT2pBJSVc+kHWBH253hXjkIQZekIlx7s4v7SgNr84KxEo13VDO5k2BuJjy8MjKb9dD6krG0qBSCNiARDqFWt0iAw59S2dfDEhty+kTljFM7TXT4woL6RGSu3OFBcclspJcyiE5r+sMqXpryhsrJO+kCUQTFbkm6nTJiXcQl1SkHID97lHjviKmckZ99mdSpl4KyqQdCBflHsrNcxQYgwBQsVPl6uSSX3inL2g0MUGLYU6pcJI/EPqrvC8TbTNMcnhK8KJpUzVZpDTbS3XlXS20gXKjeNtpPAD6AwrTpmtoV9NVicRZNv5o0CDY+ZjeMH8LMNYLeW/SpIOzKjdL74uUekaBRsMIxZVZdh4ZksLDqifKKarwqeKhkkJ71tArWnxWGSsja0d2+pRdwR4YiUpEutaS20m1nPvgDlG5SlHlJJsBtpJt9pQ1MQ6GwmTkWW2EhLSEjKgDRMTJiZyCzirdT/AAiJhmGw0sIuLu5lMYjXS1UxN7DonnZWVdaKHWmlpPJSYo5vBNFnFlTsgwSeghczVgyTlVmA6/uhNNriJmdQxnBUpVgLxNkZTyGz2g/JRIzNGLscQn6ZgamyywZeTbsk7LSCBBHlRJoARYJQNLROCQ0yAgbDlzirqDn1axsSNolMpooG9wWUV1RJO7vm6w/2jOPkxgqfpeDMIz0jTsSYhYLzk/OLAbp0v94/1jrHifinSMQVJapyQ4ljEDxJK0M1LKVHmUjpH3/1QqXVqbxEk65LoeckazTUtMLQSAlSTqi45x4vwVNVBVLmTNtvtMtKs0+VKQrNzSIcbEZGcW+ydbI2NwiA3F7r0BSuJWOsGzd6biatyjrR2XNlxN42bAP/ANQjGOFHW2OJUlL4opCSA860nJMNJ5qA3No8bCtTCUntH3CANcyr2HWILOJmKi6tElNFbrfX7XpA8Mm5snC9ux3X7M4W4jYd4o4Ulq/gqebqFKnU90g95pfNCxyIgYxjhmVrci9LTCQW3f8A9FdRHif2CcW1Kj8Ra7RZda1UWoyweel79xt37wHIx7ncmveGdCL2tcxX1TGlxYdVY0xcAHtX5vcfeELuD8azi6xKuO0espyOOtpvkPJY84yGa4DYhUgO4cDFZk1/onGlDNblcdY/UHG2E6fjSRdkKuyhYN+zUoXymMBqnAt/Cs+r3ZE5LtKNwqXWQhY/hFb/AFOoomgNOnmLj9wVYnDqWt1e3veRsf2K8zYJ4Apw7NM1ris600zLqCpSiyx7SYnXfspIGwvHr7g9gv8AJFhKuYwxwlmnVjEo7RTCiB7jKJ1Q3/aOkdoSP5IqTMYawXJvVW1hUKk92y0nqkEkCKXEuFcScRakJriDUi7LIVmTIS5sgn+t1gXY1CCZZ5g5xGgGwHQD8yUkeDy5RFDEWtvckm5Pqs5w+w5ibFdbxW8yUqqLpRKBQ2aGxjmKG3gw4FBQsNjzHWNZFHYpjKW22kttNgBKAPCID8VtNPNrQr4EjeMnNVmeq4p/0LTw04ip+GOS83Brta7MhRIB0v0ET6mhL7iQlQQE90AnbrCqpLGm4hfSpNswNr84iTagqXK16qByg9TzMbaJ92tI6LKSNs4gpkKR26Ai/ZoOUX5xOd7mRaT3iQQfOK5PiGm246ecPduVoCSdGrEecOPbdNtdYo6otYLzCgtVy4ix15iH3agBJEoPfSDpeAmWn1SLgVfuOIv6Q+3Vy4kpBPP/AOIppaLvkjZW0VV3bFEFNccqE6yhsFa1r7tuflHqngZRHaThiYfm05VzT2gPK0YpwFwM7W8UNzM4hSZaRaLilEaZjyj1JJttSUullkBKByizw6gvKJTsNlBr60cMxDcqcpzTSKyeXmWDeHVzQsbcorZl4rVpteL/ACKlDk5Mu5miE9NYAawB2j+nKDda7Nn0gFrKyXnzytFZiQsGq2wwXLlTUpCeyUMqfEb6RKMskm5QLem0RqOqyF9LxZdqm5AF/KKV26uWtFlDXKJsSUp9bQz7mkoIKUj4RMW6bG+0RXHlJ0HwgEVgoi6akg5gCf3RGcpjZ+yPlvEpcwu5hJmCgFW9o46IcoVa5R2b3yJ9LQ0qnsAgFKfWFT2J5VkFLrS0EbkCKOZxZT0g95Y8oe+GleNFHNVE02KuVyjKdMqdvnDLlPaV4Ug/CBqaxrIoQbOLJ5Q/h/FJqZUClQTfQkbw1LTSRC7kcVVFIcoVsaY1c91N/SOfRzf3E6eUWCE9oAesJWkg8rGGMxUkNCge5NhXgT8toWJRu3gT8okrA5bwttIUk3sAOcJdJlUByTSoaNp+UQ1ySQTZCfS0Wy7jw6wy4CQcwEc1I5oR85ZKFXiiqC9T5RbleZoHqIoqmogm2kbWQaFYdh2S5Z+6wnrFrLuai8D8ivM4L8hF5LqunSKMixVy1wLboipaiHQQdCIROm9RPkIbpCiV2B0HKGJ94t1JV9ekV1U26nQGwRRRnrLSBtpGr4DVeeTY8ox2iO3UkiNawAsGeQQeUVzhZqfJujPETvZrES6A9mbAG6hYRUYodAcGu0PYfmMzRA5C8OscAQo72XasC43YZn8M4ocmKihKWp85miIzd1YeaUk635dI2/jvT5isU8TSlrdVJq0vrYRhUv37HmYzWNULaeqJbsdQsrOC2QhNSj6kAgk3QfnBngmvrlqpLkEp74HrAXMH3SbSsDur0UIvZIpZKHU2BFiPKKd7srw8JtmuhXrdrh3U61ItPtvsJS8gKFyNAYhynA3NPJXiSfbEsk37Ns6q8oGsMY8ckMINTU/POANt90Zt7corKZx6W++572yXE3IbJMej1GOUdDEw2sXC/VQ3YbG92q3ml4foOHWAmjyMukI/pXEgn4mB3F3GGl4dbUyh5LzydOzZ2EYxini/PVVosy7vYtK0sjQmAh1a3x2iySpRuSdSYyNf2vmldlg081NhpmMGy0KpcXqziKfTL0hsMJdNh1t1i6Zw/Ua6+iUdnUpW4nvOq2vAZw3pRdmnZxxPcT3Ukxp7BCQDsoc41HZzDTLC2pqCXEm+pT74w5haeaqFezw04oqm6s2VHfLE6icCqfS5sPGooUobExaNTC1HxqI9YlpXoLqJ87xuHFjm5cuiqW4PA3VXdNwZTpHVM6m9usTXsOU+YSUmbB01sd4HEugDfWFpdAJsTED+nUgN+GFMbTACwKec4f0da1EzBJ9YYVwsoMysKePaeZibKrSdz8YsWSgjf1gmwxsN2tS/AwncK1wJSaVgcOppoyod8Q6xaVisom37gEJtpA6h5CeYh0zCFpyqsRDxlcW5eScZTsYe6FDqDgMyhXLNB8V/5Db/ALMZhUZpKZtsZtMwjSVKtQGTt3BDQPdcn3jVqznEh1UfOIaF5qX5gaGF4ofssjbWITT3+SjbkIyNebkrR0rbNCp50/mzkZniZyyjbr8o0OZdzS67mM2xUcuYx1KO4pLj3lEknMyRaHXT3uUQaav6sfhEhxzMdNIvaD/IFWVo7hTrbh25XiU2o/GILZ72u0S0KvF9dU1lIBvDma3nDKTfWF3EPXQpzNbeFtPrYcQ7LrLbrSwtpY+yoG4MMk6axwq00hRukIuFs819B8aqdLTn0hK0HGUm0GplMwQlucSBYG8SsDYHewBPVevzszTq3U6VIq9xp0gsOKRcWLht0jDVEKAzC9uYNj84IOHeLFYGxhI1VOf3cK7KcTmKs7KtCDeJIlBcCRqnHSzOgEJddg2B6dL72KrHH1zq3Jh9QU7MuqdcP9YnUQlKCpSUoSpSlKASkDVR6CNVxhwVqcziITeAJdFSodaPbyrjaxlZKtSlXSHGqHh/gy6mfxTNs13EyBeUprHebl1dVnygSxw3Xp03arDoaRskbrkjRo39D0tsbofa4S1uXbl3avOUukmZQFNsTToS5lPMiLXG1SpOGeHbODKXPIrM+7NCZmplBu3LEa5UmM2xJiCcxVXJiqVxxUxNzCiblZytp+6kdIiJVyAAHlC5gBYLBYjjFbiDQ2cjKDewFteWu5snluEkx9mhHOPgbbwCqglm8fesJUq0cKtNIVIl3hObXSOZvxhObWOXJd+vOOE3MJzX3hKlb2hbpEoqAhJWIQT13jmbWOQpebWOFesIvrrH1x8YVDdQazOOyskoSSSubmFpYlUDdS1aC0b5TKMxgbA9JwrJqCnigTVWdTu48rXKT5XjzTjaoTNKbptQp5suRmw6knkQY2TB3Een46YD7T6WqiQPeJdw2JVbcR5R/wAh1s7XtgaO6Rv+fvstjgNFnphNyDjf1G1/Lc+qNpWbXJoWljIWnBZbLiczah5piiqODsHVV4uVLD6pd06qXIv9mk/3RtE/tSnRQIPmI5orW/4x5rBXVEJ+yeQrp9PG83cPbRR6PhjBmH5tubpGGfeZ1k3aeqD3aBs9QkxezVTfqswqYnnC68vQq2AHQDkIqVabnSHWneSdYdnxGqqNJnkhCyjiYczRr1Oqlr20jOeN9eTRcFPSjax71VT2aE31CPtH5QYVzFNNwzIrmqzMIbSgEhoHvLPQCPMXEfF01jOpuzj105h2UqyPsJ5fGCpKR08jQBe5VlQ0+Z/Efo1upKGeGSFN44o0yCbGpoSk+Q0j2zUT/lKav/tf8I8l4aoyKPXcKoWUoyTaC4o7XJvHrOrHJVJnmlSgUqGoIsI03baIw1LI+jWj6FVdJMJ42yDmX/mFGVeFS9Rm5K4k3W2gre7QUT8Y5mzbX+UfKb0vY/KMUzO05mGx8lJc1pFnC6cmatPTbeScnH3W/wDZhVk/KGm3LacoZUCPsn5RHfqDEiCqcfZYA37RYEJxHF13G5XMiaBZosrBSri5MZvxi4gowrRnKdIOJVVqgnKAk6so5kxVY4480+jJclMLlM9PWsXvsNHqOsYZNTc7iWpOOzTy5idmTdx1RvlEXOGYVPVztYG3J2H+8lMZAyGMzzmzBqpOGZMzU+X13Uhi4zH7SjuYMQDeIdLpyKfKttNCwTv5mJ9o+jsIw5uH0jYG8t/VeT4ziTq+rdMduXkElVwIZJu5Dy1fhEUn62LUKoO6eG584cSbc7iGQYcEEhBTyT01hxB10hhKgIcSq8KlUhJtuYdBuIjpPpDqFjb8Y5cnQbcocupNlIAUpCgoJ625QzmhxKrbRy4L0LjzDtQ4p02j4mwOgVSVEghh+VQr6yXcSACMvTSMYqtLnKPM9lV5OakXL+F5sgfOItFxRWcNOqdw3VJymrX4+wV3VeZEGuHuNVcl1BjGLUpiqnLI7RmcaAcA5lKhziO+DMbgrcYH22nw6FlPJGHsbtbQ/sfohKXuFC1rQWYLDz+LKCzJX94XUG+zI5ecXtV4fSOMqanEXCBtc1JuG05Slq+vknOluYi54f4Wf4cMzeNceMiRRTGFIpko6e+8+RobRHZC7PZbav7UYbNhUlQx4uQRlPiuRa1v9FlH4lY4wqvH9UTX8Fy9SnZB4NmdZfyduQBqoc4F8b8WJvFtJYpFOp8tQ6IwcwkpX+kPVUA09PvVOoTU7NEl6ceU6vXqbwzcxPDQDdeJF7ywMJNh56J9J18ocSu2tojIV1OkOBWmm0GgspQXc3Ed7TXXURGC9tYXnuIW6SykFcfBQOxhnMNLx0KA0ECUoToMLvYQ0lWohZVcQiWyWlcWmHqi7I1aWW24WwXAFkc0xUA29Y6lVtib9YZmjbIwtOxTkbyxwcNwvWVLmm35VtTSklOUWtttESqzgCj2aRptf7PnGc8K8WtGnM0qYeBm0XU2lR3HrBPV5vtMyEKIzDvEdIydVeEljuSu4GiQhwQ/iCuud/slEJTsfOM2nJipzmKaK+3PPS0tLVFpbqWza6L638oNK0kKFlEJH2R5RUy9JM0ohsaHXNFFK5znaK+ga1jb2XpSZmw7K3l3QgrSChW/KKiemA1Lp7RedXM/eMDmG66ZimNNTagmYl05Vg/aA2tDs5V2mjckKPIfdi+dUNe26zraYsdZZ7x74VUzjLgKbodXCGnwe2p00U6yr42PodAY/KHjDh7EmB665RMcSQknpNRS0tprKy+nktJ53j9eq5XG1NKCFAAjvX5x569oChUbFuCKq3XZNmdUyz+bqUm62lkgCx3iL8W1jxzCnspXvb0X5ePTrSQtD7iAlaSki+oi64XcFK1xFrDcvgmWmXkBXfnXWylpkHfWPe+DPY/4eUek0p+ZoaqhPOS6XXXZldwVHXaNmw9hSmYdlEy1IkZWRYQNGWEBKbefWJbsUABbGD80w3DCXB0h2WXcAOAMlwZpDhDpnatO2M3NqHiPRPlGsTEwW05Ub8xEuZmmWctiADp6GB6feeaCkuOtl0quldtAOkVcs+pJOqtYotgNkmYezLBvruDBNSHZeflAzONtvNkaocF4Bpl5aUlSim4F7jYwvDOIEvuBTLzbiM3dUhWYE8xeIvHad1JMDrI7f4c0KaHaNypavqch/dDb/DejJlD2AcSq2iiNvWJ1Pq/dFlW8ofnakcmcCwO5GxgDR0b7kxhD8TVNIAeVh+PcLO0cKcaR2sve2ZKdfjGIYnupC05StI+dvKPV9feTOJUlQSQsWPT0jzzxIoSae+4WUlLS7lBA8MZ6romwuzx7K9pKsyjK/deW8Z/m9SDiXC6kkjvDUHpFE7NhxTSL89fMwX4/ppfSpxpP1zavCkeIRnby1NuBLiVNqSrVKhYiNdh9pIGlZ6v7kxVm7MlMw6EbAWOnOFSr1z376j/2IqjMLcdJRqSdfOLGVTdIO/O/nEt7bBRGOuU7Mv2l0jexv8INuGWBpzFeJqdKKaUG3SH3CpOgQOvrCOGPDaY4gYgalwkpp8qQ7Nu8rA+EecessLYUksNOPvSaE9q+kIzW8KBskQ7BSmYg20QTVHCvrqiDDVDk8NSSpenNpSHDdxVrFZiwW8Qb/hERL1hvHVuEgEGLgRtaLAKszkm5Tinem8R3VjN1ji3NIiuLOa94QtRhykuuDszbn1gHrKrLfI26QWrcukwJVVQu/faKTFdA1XeEnVypKXMtpbUkqTe8WCZpsHxCB5tVPQpZU5ZV+sLBkVq0mbf3opSAVch1leLfQvYiI7ikkeIRCRKSywMk2LH+tCXKS2s6TYv/AGobJA5otTyTrqQTooEQgEK0uIhvUsJByzYt6xDMm6FXRNi3rCPsRulaSDsrGap7MwjvpSfhA5UsLSy13CBFqG5gaJmUkwlbE2pOjraoYGYHQpx2UjUIaOE5YquWxb0i0plCbllAJSE9ABEgydQCrhTZ8oW23PoVqEmDeXEalNNDQdApwl1NjQ/CEKbULXvYw0VzqbHID8YQtyfWdGBDYBThITy2CjncwlKd7H1hlSp4G6mbw05MzSBrLq+UEkuFJUSnzhCgVp2tEX3922rCvlChPEIOZhz5RxBCTMCjQrswi/3esUtUXobmL92mTGSxsABteKOq014IO2g2vG3eR1WDDHW2UGmLzvK8uUX0sq1v8YGKUVtzC0uaGCBlRA0illFnFW0HgCvqU4UvjX/rEepTF6kqEyL+VY19YqaxU2pWdW6+sJQOcVtSFYRPDW6oxokyStIv/wBI1jAE8lE+kOKSmw5qtHl5fEN1CimloASPtK5xUTmPqx2hUJ15snbIbWiqkIPduokuLwRnTVe1cRVyUcm+zMyzmB2ziLbC7yHEns1pVpoUm8eBE41nFPZ3pyYLnNRUYL8N8aKth5QVJzy1aWKV6iEByEKIMdadCxeu6tItVFMwzMAKQ5cKBjAsYYIewrOrWlJVJuKulY+z5Q/gz2hWXqiy1ig/mr7gDjqN067x61p3DLC2OcPtzEhVGZ2TmUZr5xzG0W01BFjEGRhs9vVV89THJqvB9aKQ2FJsbHlHBUClhtKScyrZRzMb3xx9mym4SoEzWMPVlsiXUCuWUu+nlFzwi4M4NlqVIVit1JioTDzSXOzUsWQYzcfZiqE4gfbTW99LJkaahYLiTE70vTpGlqWptKUZlpJtFMnEaZZGigCeYMP+0lOyUrxNm/oNaVSYSAnIdPhGVCqqecClqOUfjDVfhrnSkONwNPZC6axstbpNVVNu9q+ru/ZF4NKU4apMtyzV1LWRewvYRkOEZiZr1TYkZMhKlqtcnRI6x7I4USmCOHUqHcRTUtMzSkjOtRCrGINH2eFRUAyuDIxuSnWS6XGqYpFPRSaa2ywhQCQCe7vCnKkpskEKPwi7xRx8wq2tTdBkG5hI0C8kAa+PMqxOhxyjMLYvqm2segnHcKgtFHKLDTQGyNkhtqEQsVZ0r0bcN/IxZNT0wsd1l0/3TEmicdMIT0vdmRbQ/wA21J1vFoeNtHaTZmmDTbuCLemq6WZuaOUH0R2e/wALVSh+dWRkYd/ViTLIqKlAe7O/qwtzji0FEs0xsfAQgccHyfqqe0keYiRxYfxIxDN+FW0rIVN3wSzpvvpFoxQ6qUfzdwedoHG+NtRtdthlHwjiuM9XdVdBbA5i0A58J5rhDP0CKRh+qnZlSfWJTOF6qsWCQL9TAYnirVpggFQSYnyXEGpOKGd+0RnTQtPNPCnmI5K8e4d1R15LqlpuFX3g3n0qkKE00+e8hNiYzuY4iTLLOZyZG+usXE1ixFVobTmYE5ddYbdUxZSGpfhpbtLtkHYmmcyyAecQUT6U0xXeF/WI1cmw6VZfxgGq9bXLEpCyE9L7RmKppdcrQ07RoERuTIcZXZXqLwDYqPcJPxiZJ1QuAEqNz5xU4pmM0qTD1OyzUshs5V8g8MthppEh1YGxvFPS5grSCd4nuq2v0i2of8jVBq/8blLQskxNaUCIrWFaCJaF7WjQAKkU0OctoVn1tEYOAc46ly47sFYhcpWflHSQIjocBhYVfneDBQ2S73MLFr+UN8xHfCYJddXdMxbWqPLGVpNZqEpLK3ZbdOX4dIrXXVPuLccUta3Ddxa1ZlKPmecMA6esLTptB3SZQDdcKLawpMfEkxwaEQtl10sHrHQq8NFQJ0j694VJcJZVePiokaQgbR0npCoSUoK+No4VQ2VaGPs1wLmOshLk5ntCCq43EJUeUJJtC2SXSibc9YTfWElV4Te40hUicCrwoQylULC97xwSFQa3IpqMi6w6nMhxNiP4xk0ymbw7Pd1x1vs1fVPtqII9TGyOm41MD9Xojc4FGwBVuCNIqMYwSnxOPLJoRsVcYPjM+HSEs1adwoOG+OGIaQlLc063UGU6We0Pzg4lfaIbLY96o6s/PIu4jKprBmRRLQW3/Z2hhOG3mxo4o+ojzer/AOP6pr7xgOHkbfmtlF2lwqYXlaWn0/Za9N+0RKJavKUh4uW1CzYQIYh9oCvT0spqnMsU8H+kScyoEFUF775+UNjCqnD3+0XfkBCU/YOrc4ZowPUhK7tDhMYuw3+R/VQ5jEU9WJjtKg+/OTB2zqJ+Q5QS4foq3HUzE4LrHhRyRC6ThZMuR3AgfjBZKS6ZZIty/GN1hHZqnoSHu7z/AKD0WbxbtHPWsMUYys+p9VQ4plVtyjbjSSVMnNpv6xFkOM+JqOylmVqAmGUABKZhN1ADzglqLaZtopVzgEqeFQuYUoIIudcu0M4/2WbicgmY4B1rEHYpMFx9lFFwJ2Zm7i3L3RM17QWKVG2eUB/siJn5esUrR+klB/cEALeFilXgcic3hsqGzotGVP8Ax9Vfdye6vh2qwrmw/wD6/wAq9qHGnFswhSRPMtX3KWxpAFV6/Uaq8pdTn5qYUs63cNj8IJjhcOixSv4wpvCyGT4Ep/GJ1D/x/K0/avaB5C5TU/bKjjH2ERJ+QQbJU+YmiAhHZI6kcvIQbUGlIkWbJTYncndUTGKc1L2sLkc4lgZE6bxv8LwSkw9v2Iu7mTv/AAsbimO1eIH7U2byA2/lOJFukdP/ALMIB89I+UvTT4RctCoXFJXfmYiqNnN4fKrGx5RGWbuXJgkAUhCrw4mGELCdodSra2xgl1ksaGHEfOGrwtKrQq5PphxJsLwylWbyh0KA2jlycSSd4cSrUQyFXh1Cuvwjkt1ITYjWwhRNhaGcxEKz3AAhFwUym1+pUKaMzQ6hN06ZULKcl3CnMPMc4equKaviFSFYhqk7USjVPbuEhJ9IqjvHQY4IrBSw4CND/wBYUFaQwjbp0haTl9YVJonLabwtKrD+ENBV94WDC3SWToXYQoK5wyFWj4K1JO3KOISWUjP1hQXcEnlDAXC76EDcxx0C4BOoXc3hwrJ2iGkkHnrDwN4AG4RWT4XbcwrOIaCgd44THWSKXLTzkhNNTMusodZUFJUk6xo1A4gN4heW1OhMtMaFCb6LjKnL3htDimnUrQopUk3BB2isxDD2VTeh5FT6KrdA7qOi1+bHvEySvfnflE2kuJRMhA1F+9Alh6szCqSp+rAraTo259pQggp04y80HpRwOIJ3B59DGAdI2KodEXAuadbLXhhkhEgBsUcuU8ONBxhWVy3cUIxrjHxnkeDMrLzmOWp1NMm3eyTOsIKkoV0V0jTaTiVLawzMGyFGwJ+zFFx14byvE7hViShzbSXfe6e47LEpByvJHdUInExygEH1USNro32ePReb617YuBHZftJKozk2gi47Nu/yjC+KXtlu1KRXT8FUGYKFvJWuanO6FpB8IHnBLhz2eVMcPKKzZozDLakv2QLqIURGfYx4SPSMy2wlhS3XnMqEAQzG6mbLqC4eZWyGATPpwWOANtV614Ne0DQ+LuHJV2kOolKpLspbnKa8oJcaUBqU9RGgTFSzpAByfeB5R4DRgGcwrNMzLImKfOsfo5iXORaT59fjB4x7TWKcE0lS8Qy8vXZaVFi4e46R5kww/M59odQeSiVGDSQszv5br1dUZntWg2DlKdUqvFU852zJbeJWORB1EYpw09p1zig08uQwjPyjTWgmXFDs1noDzg8TUqvVv0gRJNndKNVfOItRxI3ljxYhQYeG9ocw3CcxxOzC6JM0qjrInqg0Wi4k6MNnc+tooeF2Hv5FSjNPlXn3Zdu9u2WVFSibk/OCqSpiW2wLE5jck7k+ZiWiTDboIFr9OURuI61uSkZW7otk50qazIWQpO4vtE5VSUtlQC8quYve8DDDqmgFJNiPxEJXULKJQT/hEhs9go5hzFTZ6ZFjrcK5g84znHakzUq7LP2Uq2Ztfn0gpfnbFWZVidQf4QF4jDtRmGkS6cxJ2vyiPI50oyN3KfjaIzmdyWPUzA/8p8QoadJQy25ndUBqmx2gH9o7hk5hnELdbpiFKpVUT9YpKbBl0DUHpePU9Ko8vTUWl0ALUO+5zVEyp0iUrUi5J1aXampZ3xNuC4PnG3w3CnQ0+V57xWUxHERLUXb4Qvzxl5YlQsbX3g0wLhKaxjiGUpVOQpSnlAzCgNGmhuTHomp+zbh2exGqdbddlqetPekm+SuoMHmFMC0TA8qtnDkmGC6B2jy9Vrt5w82gkc/v7Jo1kbWd3dNYMwXJYHpTspTUj65eZxfMkC0XgVaFKVYGG1n5RasiaxoDdlXukLjcp5KiNzeHArQaxFQsH4Q4HPOEdoEoTjp7ptENaiUbw+tVhcxEWq4PWGHbp9ugXzrmVsQLVZwETF/uwQvOd0jy5wH1t0p7cf1TFHiw0arvCj4lheK5l9FReLLriBc6BUCc3WZ9rvImnbf2oLcQN9rNOk73MBc81lzAiGKYi1iElTe9wuoxTU2zdE4984+cxzV2VAJnHNdtYpl6ExGfWNL6GJ3BYdwoBqJBs4oqTjmsFI/O1GPjj2sNglMydYHWVFTYvpCykkXgOBHfwhF8TLbxFErPESsISMzwPSJktxNq9wFrST0gOQkjS1xfY84fZTcgo/HlCOpoj91EyqmB8SPG+JlTSQSEkc4dVxXnkeJpJI6GAtsW8V7c7c4460Tc7xG+Eiv4VJ+Mmto5GiOL82lXfYHzi1leMakpBdlzb1jLVNEqAQCVE6W5xay+HJ95AsyQk25bwMlHABqErK2pJ0K0tPGWVUm7jCkw09xlp1u82sfCM6cw1OlfZsNqcV0HKK6fw5OsAl5kp63ERm0lMSpZqqvLt9FqDfGekhVlZh8IsGeL1Feb1Xb4RgL9KfBv2S/LSHGaJPBOYyywnqREg4ZA4XBKif1Odps4Be2ph+ZUk2TpAxXJx9pCi53YKZh1TbJUIAMTzi3M4Wom0SA8lyac0Bt1Bokyp6bdKiTBSysqbuNxvAPhx/8AOnILGHrWsd4Zm8a6LVitETHYoUvXujaM+rrdQr1SVZKm2AdBB2yoKNuRirqCss5ZOlvKIM7QSEboeI2xOiqKdgh95AzTCEqtDNQwJVUJUqXSl8DkncwVU2YLax0PKCWkTNplJvvziumhDRcJP6ZTOFrLCZhtck4pqfYW0saEKTHGJczX6JJUryEalxKWxOz7TK5ZCcouVhO8C0vNydNFkISB5jWIT3dAs7VwiGUsabgKklKPPrdAZZVY73Gkaxw7xzWsFESL03Msyrp0Qlw2RAI/jhmXBCSkK5GB2qY/S6DmXmUk3HlHMEuYFo1UZsmU3XobH+MHqlhqcDsy84FNkgqXfMYDeH+LXF4Rt2zoUzcaKgOpNeVinDTl3dchSR10iowFUjK0iqyrxOZhSrRcvYSA47qxc6zmnkiDG0k3VKM3UCol5Tlrnc6xLw7wVqFQl5R+ZUGm5u2Un7IMRcGv03FplZKqzQlksOZgk6BRvG+LoDjUm1lnSJdDdmwk7CDjpmzOuRdMOdFnuVjWPKNIcM/dZagvrcn3f0riNSmKiQr0w/q6664pW+cmNKqLVJl6jeelxMrB/SL1iuxBhenVVtL1LCGHBa5RoD5RExTDmSMLmkacky14kkytVTJVeaQ2OyIUIs0TippF305FcoqW6A/KWKXkmJbEqsfpV3HOMlLhs5PdYrBtLN0UgO9kvtGlFK07EQV4cxMZw+7TSj2gHdV96BOcRKhi7K1h0cjziBJzy5KcbdtYoMTKCOpoJg9w0O6kxxSxOBWwtLzEbxLaWQf/AHpAUziolsFto6gQpWK5kp0bF43cczXAEKzsCjcv3tYxKZeAsSdfOM3OJ54q7qLR0YhqCtjYQ6HnohIatXlXkjvE2B5RJVUG5dJJWPnGR/T1SUAO0IivqFWqSkn68256w29hfyXB4bzWk13EjakEdt+O8XuF8VqVTEtKXmAjzXVZ6dWvvvqJv1jS8GTziKc0FquoAX84jSwlmqdjkD9Fqs3PJdbJBuTGbYomyHiLm94JWp4Fo5umsA+J38z9z1iFJHoVKjJzKbTJtSkpF9RtCMRvkyet4rqa8bg3h3EDuaR1hyMANCV5uVCo7mZAvFs6s2AA5RQ0VeZAtyi5dWLJvE6i/wAzVDqv8ZUplZtElDlhrEBtdjvEpCrxoVSXUoKMKC7bQwFX3haVWHnBBcVICusONrsdtIjBUKSs2sdhBISp6TmhRHXnEZl4AEG2nWHKU69XJt6Vw/JTlVfYF3kyrZWG/UiJlLQ1FS7LEwlNuka3c2SydCI62rleO4epNdxdWV0ug0OeVNNC7geQUBA8yYOpPgNjh1etOl0f2nhEufB62A2kjsfkm3VcDd3hBQFhCSCL3jSEez9jT/d5MX6vCO/9nfGayLtyAH/HEMGinH3U2K6nv4wsyvY+UKCo1Eezdi/munj/AJwh9Ps0YqPifpyf+cIT4Wb8KQ1tP+MLJ7k6x8TcRrR9mbFJ/wC900f80R1PsyYnJ71RpiR/bEd8NL0SfG0/4wsjtHL2MbCfZhxHpaqU39YRw+zBiL/zemfrCF+Gl6IPjaf8Sx4qA84STpeNiHsvYgG9Xpv60OI9l2uK8dapw9DC/Cy9EnxtP+JYuown/wBmNs/7K1aJ/wA+SA+EKT7KlZ51+n/KF+Fk6LvjYPxLEAdP+kfKUQPONy/7KdW54gkB/dhKvZSqh/8AEciP7kd8PJ0XfGwfiWFqWSf3wnLfz+EbmfZQqg1/lHI/qQtPsp1LniOR/UML8PL0XfGQfiWEFoHQi8fBlA3SL+kb1/2UaiR/pJJfszCT7KFQ/wDyWS/ZmE+Hk6Jfjaf8SwxuXbWNUJ+UOGUSBokD4RujPsp1BG+JJL9QxJ/7Kc+pOmJJO/8AwzHfDydEnxsHJy89us5CdPwhlxeUR6AmPZQqVtMRyX6hise9k+qZv9I5E/3DA/CynYIhXQfiWGlWc2j73cK1teNwb9k6qlf+kUgf7sWTfsk1XL/pDI/qwXw0g3CQ1sPJy89FgJO0I7QJJtHoV72R6qQbYjkB/ciud9keq3N8RyH6kG2nf0QOrIT95Ycl0FQBht1GdWkbm37ItWCr/wApZH9SJbXsh1Y/+JKf+pDohcOSZNTFycvPRZNuohtacgj0b/2QKsof6SSH6kMuex1VydMSSFv7EEIym/iWdV5xW4SdLgRztQPOPQznsbVe3+ktP/UiIv2OKuFf6TU/9WDEZQ8aPqsCUoq2hixKtY9BD2PazawxNTv1YWj2N61muMS00/3Y4xP6JOPH1Xn7w7j8IcQo35xv7nsb13TLiKnfKPh7HNcSP9I6d+rC8NyLjR9Vg4It1hSTcRvQ9j6uAf6R0z9WOn2Qa8B3a9TDC8NyEzR9VgwURsIW2epjcz7IOIR4a5TD5XhP/ZHxMnw1eln+8BHcNy7jx9ViqNN+cOJVY2jZv+ybitN8tSpSv+YIQv2VMYJPcnaSr/nCO4ZXcaPqsfz9Y+BvtGrOey7jkKISaUsdRMJivX7POPZdxSPopl4JNsyHhYwTYHu2CXjR9VngbJF+ULS3beD9fA3HjSbmgFVvuugwzLcFsdTz/YtUBTKrHvPOBKdPMw+2hmdsB7j90vHj6oIQY6DY9bw9iylVLA841K4xknKY7MX7FStUO26GIjTwWm6Ckg7G8BNSTQ2zttdONIcLhSAY7mhkKF4UV6xHsuunM0dzaecMg33jo8XrHXSBPhe1tYcBuDfeIwVYw4lXyhCUoTvqYXm00MN3Fh5x8Tp5wKJPBdtOcLCtIjhULz6Ry4hLPXe8SqZJKm59httntypY+rHMRFZSVKAPzjQ+H0ghUvMzDbBSEd1Tytyf6vSKrF6z4WkfJz5eqnYfT8adrOSmTMsAwGAkN2TZKANEeUDVDkZqXxI81KrU1LrbK1tr2J6jzi5rily7/apJC0ne28P0JAqE6mYl1sqcsQ4hWhR6R4hA9zqoX3uvU3NaKc9LKun6m5Iv9nPIUlJOjg/jGjcO8TNVmTckX1JcmJZOZIP9IgxSVjDYdlCVjMn+sNTAShicwvVGZynEtvS6syRyKeaT5RrIi+CTMdlQytjnjyjdRcaYdVgSqvpCSaJOuFyWeCb9gTug9IDXKdIztdTPPpbdTLt/VkAGx6xuFQxRT8U08ns2ll1NpiUeHPnGR4h4fUdcyp2mTtQpKlE3bQrMj4CCkcwOu06LSYZjAbCI6gEEC1wgLGcvIOMOqneyG5LirAWjGVcIXuK0+ho9pJ4Yl3c0zNFNlTdj4EeUb8eG9DEwHKi9PVdaTcNvrs3fzEEDxlqZLp94Dcu0gZW202SEjoBDHxZjdmZv+Sdr8QZURcFg0O5PNDdAwjJUGQYkaVLIlpSXQEtMNiwSBzPUwRsssyyAFAE9B1isfrzStGVttpt13iN9OSybFbyE+p1iuMxLrlVvCFkQJWM3eG/SHFtpQCQbpgZexbT5VGZx9BSPOBKtccaNIOLbSXHFAakJ0PlDgcTsEORaLMzuS4Btb8Irn6khpKluLS2lIuSo2tGEVrj7OzjLyJCQEu4bhlaje3mYAJrFlermZurVB9bBVmyIJAh4U7zqTZN8RoNgLr0PUeINNTPtSqJkFLiglTidQ3eClDTCWgZUhaFAHtPveYjzHRB3h3bk231vHoDBM6iboMvkXm7MZDfceUXvZt0XxDo3C5tcFU+Otk4LXg6cwiBBCeUKK7nTaGwI6ogAxuAsg4rinCDHA5fnEd5fIRbYYwvP4lfV7sOyl2/0ryhoBAvkawXdsjawu0CgFVt4bUu17fGJ+IJOXpk6ZeUcU4EaKUeZirWRlgWStewObsUroyx1juvgqx0hxLh5wwm41PyjqVW3hHG4RAJxbhUNekR1LhS1jWGFXtpDDk8Eh090+kB1ZN+3v0MF7yu7prAjWbWeO2hikxY6NV3hX3ljVYbu+4fMwGVNFlqsNYM624ELdJ6mBF0pMynPa2aIlPe4RVOjSht+TmCSUsuEdQmITsrMLWLsuD+7G6U1iVekmsyG/COUSxRpBYzFtI+EaEQC26yhrnX8KxBqWWloXQoeohKsyNwfLSNucoFOXoW0fLeIU5hGmrSSlKQfSA+HtzTgrv8AtWN9oQq50PnEqWsNToDvGkP4DklN50CB6doMpKqISDcQ3JHl5p6OqDjsqZpaTzF4eCc17c4gzzjLKylkEGPpKcJXlUbg6GI9lMZJdXNBpap2sMpSm4CrnSNXdYRKyxJSO6ne0U3Dejh0Lm1p1tYG0WWM51MhTXlDQlJA8oq6uQPdlCvaSLhxF5VXh6cRNVCZyAaG1wIRiJCXnUJVspVop+GsyXhNOKF7qi1rrt59kaeKAMOVqtKeUGAEp6oUWWUxLhtlNyRsIrsTte5yhyIygDa0FUsUuTDANiAIpuIIHuirdN7RMo3FsVyspjBDqmwWz1F0IlCb7J1jNK9MZi4SdAYOqo8VMKHK3WM5rzts4vCwEE3T1SCGhV+HpjLNOHzgwZcCgMp0MA1AUBNLF4LmFZU3B09YScd5JB4ArqXfCSAdQdohz6wqauPhCGJjKoX1HOETbic5Wo2SBc67CK+YWIUyMixUlh4JN77dYeaxhI0x4GZfQCNwDGeV7E7j+ZmRVkbTpm6wHKLjrxK1KUeZJ3iMcrxa6qqnFmxutGLr0DMcRcPTYSZxKHcuxIivnqpgzEDKmswlnliyVg2sYx5k5E6jTpFbVphtpN1KCbbWMMxsbnsAoLsUL/EwFLxtRpqjT7jcrMCYllElDiVX0ikwtPNmpKYqeoc7oKusOyc0Zx9oqmFLYbUMyFHcQa4upmGatQ2JmgudhUGALptbOYtxG3IWlV0mVzszRZcwq4qhVZ+Szky8yLt3MSqK6mXqlVbc2cQogQNImXOylX7kvMEZhflCnqyGp6ZfF/rG7fGIcpcRlRGY5QOiVJzy2pklpZR2atCD5xtODOLL66emRqDpUUpsFKPKPPMtP6KUrrcG8F+CGhOPvzc0vLLyybk3sDDE9O/dpsU2zvOstonKy3NE5VXvsesLlJxSEDvWHJMYwOIqTU1MygzNhVkm8Ej+M3aahlc2UgGxSLxXuw6W9ySnIMjH5hyWuNSLj7IW73b7DyhbEmyV5VuJCul4zOU4vO1C0uShIIAzCCSRYfmUpf7UqzC9wraJNbikVI0BrSfVWza+RxsEa/QiF6jKoeUccw+2pspUkG/OKmSqcxIkAqKgORME9OqrVQb3CVjcXh/DMYpa48MizuhUls+bdQJOlmVaCHLEg6RLTKoI1AiWrvGOoSBbX/rGijha0AAIzIook08hC0SyU8rnpEtKRcEXj5zc3NzDvDCAyKEtkK1GluUV04hJB8otnDZMUk85lBN9IUN0SEoZqaczigBpeDfDK+zk27chAVPLC3emsGlEsmVRboOcV9XpZTqTUlEgnLItqIEsTzN3QL6kxdKfuLDeBLE72V1BOtjEB4BCnjRWdLdzZT0idWBmkFZtYq6G5mQnnFvVAVU9XpHDZKqWiix+MXDp7vpFFTHCk2HWLdxzKjQ6RJoz9s1RqofZOT7KuRiY0q+kVrS9dOcTW1ECNKqBTQq+ohYIA1iOhcLSrS9/xjgFyfCrnTQx3t22TmmFBLY8R6CG0q0uYq62zOVRUtSaFLuztTqS8kvLtC6levlFhh1I6qqWRDmfpzQPcGgkrauG3BCW4jUyYn56pFmlupyyjjBupSud49HcOMA0nhlh1mkYXYS1bV+ZI+smFcyowFez5wlqfCnAiZDEMwHahNudu6wlV0y5OuUGDWuVN5EhONU1ZL6EHtSg3LQ6xs542B5p4CMlx6HzKxtbWvc91z3eSvBSmUTC3mmW2n3tFuNjKV+pEIclS2LLK7+aoDcEzddcwLUkIWtya7UpkHpo5SsHexMWFJq7+G6HLtYteccmVL1UrUpHmeYhl9M8Pc24JBtbmVBFS1wF+Yur1SB1Vp5wgoHnb1h9Ljcw0h1hQW24LpUDuIQtJ2GpOkMgW3RlfNISohKUlR6DWJCpJaBdbK0jqRpHmrjVxmxdOcUJLhXwRDLdemEBdQqTicwlknkOmmt4tMOcOOOuAsV0KY/lnL4wpM3Mpbq0tMJ/RNnxFJ8omnDyGB75GtJFwDe5H+9VIbH3blb6WU8wI52SeSR8oh07GeHa7iep4fodVYm6xSdZyTQbqZB2vEdrGlCqdTq9FodakHa/SpVbj8sVgmWIGiljoIgZX/hPX5JMhVuWMlszaRfYECElhK1BKEJKjysBGM+zuy/IYdxPWcQcQWcbOz1Sccmp5KsstIAX+rRyAAEC3tCe0JhOo8GcZynDzHEkcRMMBLfub4Dqdr5D1ic3DpH1HBZrqBexsLomxFzrBejVMJSbKQkHytHAhAOiU/KMr4UY+puHfZ3wbXeIVbbk23Kantp6fd7zq/MncwSYF4u4M4jzZawJiOn1h5lQLrDTgzpHW3SGpKSVmfQkNJF7aaIHMcCdEbmXKUhSkAJOxtHxaTbwj5R544A4wrVd9pbi/SavVZubplNUgykq8u7cqL65RyjTJ32hOGdMrX0RP4xpbdQS52ak9qChK9rFW14KooJopeGBmNgdAToRdGWOBsjrs0/dEJLKT9kQ+0W5hlDsu4h1p1IU24g3SsHmDC0tdYgkpuyidgkbpHyhAbT0Hyihx1xXwXw1mJeXx1iKRpUzNfo2FrBXbqRyERaXxVwfiGpTMlh/EMhUJiSlTNzAYcCg2z94mHmQylufKbdbaJSw9EWJSjoPlDqJcOGyG8x5ACBbDuP8PYsoMxXMOVeVn6NK5u2nWlXQ3l8Vz5Rm/GTHLGN+C7lW4T8RKdh2XFSQ0uv9oOz0V3mwrqdRDrKSR8gYdNbag6eq5rTdbl2IQfCBysRC1gsgdolIvyuL/GAmtY2kcEcOJWqVuvU5t5FLSmVnpx4IbnH8mixfe51jIvZJqbFRkMQz1T4hN4yxVWZpT89JImQtFPbBJAbT0tHNo3OhfKdmm2x1RhpDSV6NcKXOQ+URnJUH7I+UBT/HrhvTZeYeqWM6QyiTf93fCnhdtz7pEXuKOKWCsFUqRqWK8SU+nyVUbDkk4twXfQdlJHSG+DM1wGQ67aHVIWE7hXDEoCsDKkk7RapZ7BNlpSDb1gBr+OqPiXhJibEGAK1LVGWl6c6WpuUcBLSwk/Ixm/siY9cmfZplMQcQK6SiWnZj3ipVJ7ZIOxUYM0kj4XSnkQ23O5ShpAW/u5SNk/KIbjKSrwpN/KADCftC8OMe1z6IwfjCl1Kp65ZZLgCnP7PWNCSQlKluEJQhJUsn7IG5ht8EkJs9pB8xZNvBvYpAlxbRKflDzaAnTKm/pELCuKaHjWUemcJVWUq0vLuqaecl1hQbWNweloZw7jLD+M11FOEatK1VVJeLE77uoKDLg+yfOBLXagg6b+STIRqiNpjOnMhCbJ32EIWAT4U29I8t+1VjWpYX4scJW5KsztLkHpkKnUMvZG3U317TqI3rD3GHAuN8RzVHwjiilVKqMHvyjLwKvPKOcOzYfLHDHNa4cCdBtY21T+V2W6JFoSfsJ+URH2U691PygexvxpwBw3nBJY4xRTaXOqF/dluAuJHUjlDzvEnCLuDjitnEVNXhvY1JLoLST0J5GGmwSgB2Q2OxsdfRNuYbXU5TaRfup+UONtlCc/Zpy9dIz5j2hOGE++y3J44orq3XUoSkPC6lE6CMswziqsTXt4Vmhrq06/RhRA6xIdp9QL7KCYsYqCZ4fmBblaXagi9kLYna35L06jIoapT8oWWUq2Sn5Rl1e9ozhlg+rmlYjxlTJWoIUELaDgV2auijyjTqTPytVkpecpsw1NykygLZfaVmS4k7EGIMtPLG0Oc0gHa4RAG2oS0SnaKshtKldAIUqULZHaNo12sQRHnr20+MTmBMDSmH8J11umYiq862ic7Fyz8tKnxLHTSCT2cm8OYT4a1eeluIz2Lqal0PztSqL/dkVZQS2CfO8SDQyNo21TtnGwFjr5329OqPhnJmWvlsA6JRr5QoSZCQtTQyK8JtvAlgTjTgDiVVlU/A2KKbVZ1lXflkOALUBuUjmIyjhRjOu1j2qOK1DqNamVUqQp/5lKuKu1Jn74EAyklcJCdMgub32uB+qRsZN7r0I7K9l+kaCbjS4iMttB+yPlGN8EsSMYM4e4kqPETifT8XSctWl3rGezckkk2YUeREFErx/wCGk7NMy8rjWjOvTDoaaQl4XWs7JHrCvpJWPc1oLgOYBsgcx17AXR62ygnYRMaGXwqWPRRhIayAeYBHoY52mU6axDOqAaKXmWkaOO2/tmIE1MKUVJU6pWXxJ7S5TA5WseKo+JG5dDfbyEunLOBAzLzna0UdGYrDU1UKmyha1zKHlIQ8rVwg9wZeVhEyOgcGZ32Ggt5qJJXtD8jRfr5J/idw6p3FLDSqLXSptIcDkvM2zOMEb5SeRjOsSezphLCmFnqlKvzzTVIly9OqWq/bgDl0jRMBT1QqLE4uqrdcWl4gFacuTyET+JGCJriHw+q2HqbPimP1NIAmFC4FvsnyMSHudC4U73d0HXmNd1KpKx7srmkgFeHaTV0VRDjzIKWlOHskncJvpeLAuA84izOCqhw2xFMYaxUgszsv4Vg3S8nkpJ5iLBMsg7LvFZjEMcNSRH4TqPRaRzLm7dim0rNjzPKHEq7uphxMqlWyoUJKx8YiqzBJlKbGkKS5rpeHFSqhoDcRz3VY1/jtHXCSx6L7PeFJWD1hJYcvcD5R8G1g6pMdpZLZKJ5QsK06wgpVvlN4WEkciIRFdTKeyZhxLYISVnLmUdBeNyo1KZo9EYl5ayglIUpZPiUYxjCsr9IV6SYWm6C4CoeQjcXx2gShq9jZKEA7xgu2FUczIAfP9lpsAhuHS/JCOI5RK0m4KgTolJ1vFVQ5Z9M+i8vkVchs3sT6xq8lhSWcQA6FLeI1IPh9Y7O4JlbgoWpoJOpB1WYxjMDnc4SLTf1mJrTGozEmsyvbVNabgWCLwMVylCbupKMrY113MGCpNKXEtglRRolJP74anJVN8uUKCfEo8z0jTmO7bKkEpDrrFq3RnG3AWAptw7rToQIEcQVGqUtCVNu9ok6AKGsbvUaaxMqV2hCUI1WscvIRmuMaSJgLfQ0Uy6O62Dz84rJ4LahWlNUBxsVj1TxjWEkgKbaP3gm5EDM3MzlRd7SdmHnV8yo7fCNAxHQEyZQl1NyoZvhFKijjKMw0MRnBT2kIVXLvZL5nFJ56xHdk1rGYqWR1vtBemnGVWUuDuHYwiZpAQgrZAv8AaRfT1hkA3Rk2QNMs2bWCRcixuYCJ6hl+aUCBv3R1g8xHPmkLyTMqFlXhVewUICqhVFTKiJFCm21b3OscX22RNF91TTVPZljlNluDcJ1iK3IuuuC6MiAdEiLyVpy3FXWDrzPOLZulqbAITqefIwBqcuiLhXN1VyDXYiygUxZUXH8xg2vpDwLkjMWD7d9h94RKEmlSSCMp+0OnpAPxACpYMrsRbTMDoIfw2dzaprm7qPXxB0Ba7Zep6XVZWryLU3T3UvMPJulQP74cWvp8Y8w8K+Jj+GqkiWm3L0+YVlcSTo2eoj0i1NJfbQ40oKQsXSQdxHpdHViduu4WBqqUwv8AIqS1Lrm30ttjVwhIEb5TqS1h/CzcowAkhoKcV1JHOMTw4M1YlL/7UaxtNdqqUS6koUNUgEX5WiDi8tg1qmYdHe5WQ4tkrzKnmtwe8IG8xOnSCqtTaQ8rmlW8DEwA24cu29oTCqvM3hO5bJzEKax4g5pKSQbHeO5rK8xCQrX90cKtYtyq0LjhtDJc0hThEMG53iOU+EpRvv0gTrYBQ+eVjaCdxYynWBKtrCW3gDrYxSYuNGq4wo6uWJ4jeyuO5r+I3hjB1DlsRTDiZwqAQdLRZVTClSqRcLCfEo213iVgvDdToEw570wooXzEVr35YTlOqmMjzTjMO6jGUwAyhoBmYWEi2WOv4HfbH1M1c9IsZWqvSyQOyXYcjD4xBY/WoWD1itFfWtOjirB2FUDxqwIVfwjUkH6txKh6wyMM1dPIKtBm3XJcn6wHzhX0ywb5VgC+l4dGM1g3Uc9n6F2w+qDFUeqsslBYJBgDxDITjbi7tlKh5xtn000TYrB87xleM59LtQeUki1+Rh+nxOolflcotRglJCzM1ZhNSUx2iiqwPPWFSMq6ZltA1zqsIkVF8ZlEbxL4fS7lYxCnMLIaNzeLrM7JcqpZEziBo5reMJSKabQmkHQlO/UwAcVKsEMFsHU7+caDMTgk5GxIHd2vtGG4yqRq1TcSDdLd7axTQB0k9yr2vkEVNlbuiPhSCaS+4Rus6xOrSyqpteRiRwvkgjDKlZdSo3hFUbzVZI6GJ0kguQn6dhFMy6vKW5mmxfZKYpOIU5lYKAeUW1MWG33Cs6WgM4hTVwcpuLaaw9GPsgFkqt2aqcUVOYmnw7kU+pQzZSDzhNReU60SrnyipCwqeIP+1ifPryNqHPlDdIDdys6s91qbw/8Ap3SddRBVLL7vlAfQFnt3B1MFLKrjQ2PSHJPGm4fAFNS7kOu0UuLar2LKWGjZbu9ukTXHiPK28AVWnnKrWiiXurKco8ohVLbt0TVXKWxlrdyvnCAnT8Ygv52xnbQtQ5G2kGVJwo26pKp1ZURuOUTMZLlaLQShhlAU5okW1iBGyx1Vc3Cn8MySGwCy+dnZoNlSSG0/vivkpF2qvpbstzMdfKFP53Vd8ki8EeD1pamw0hsLUrdVtonuIijJaNVWZmjQKZK4RZkJYXQM9t4Zapud1SUIAI6CDdUt2oCUjMT5RETI+5vrzotpqCIo/jXEkuKZchuXpyW1XUOeoMUWKplhCezlwM/2iItcWV1ujMuKTYOr2TGcN1Jc82+t0kqJ59ItqGnkk+2dskurBThEoC2CVKNgIshVqgzRvcGEFtDuqlDdXlFBLTSm1NOWuhs3I6xpNDal8QNJLaU6DXTaJVZJwRci4SNPRC2FqSh2qt/SLpYZSbqUecEWLcPP1JwPUyYLzCAABeCNWE0NouUC3pBJhvBb9Rpbjspr2f2bRXNxAyvOQXKegYXGyySkU6bk3U9sFWB5xreFK5MyUuEJWVoI1SqIj9DDSiiZayLG4IhUvLe5ggbW6RTVtSJ9HDVSYwWFH/5w/JJmW0pdbULnLuIiytYVKzKVoJBSdRDGA6/2c2qSeN0OapB6wS1zDDc6hT8lZDw1IGxhyPBg5gqabQjcKeO8LtRDKTIm5dDiTcLAiSBFFhUuokOzmQQWzbWLsqAMbWkkMkLXOGtlJAuE4FEbRxSr6w2XAN4bU70vEpdZIml2TY7mKao91OkWb7gOm9t4p6go5tD8Y61ghJuUMzzpS98YNaI8QwjN0EBE8m7yjbY9IJqZUGm5ZGZaRYc4rq3YKwo9yifMFJJ5wI4oupQtyMXctVGVjxp9LxRYknWbDKQTfaK43OgVhoNVPw8o5U3i+qawaeu3SBCl1uXaQkKUEkbxMqWJmVyikNqzLULC0KxjjpZIXtAvdIph+sPW8XTgu0fKBKlzDhVcAm5gol2n3063tE2np3teHFRJqhjmEBPS51F4moJtDbNOcOpsIns07ujOsCL3M3qqbK7omkkWjoXY6xMRItJPeXDgl5dJ6wokaEvDcVESq4NrxO4e46Y4W8XKTiWqMl6nut+7PqtcsgnxCHm1SrY8P4RRY5l2J/Dk2llA7VlOdsgbHaLnBK2OKsaHbO7p9Co9RTOfGWr9DKTiOmYikWp6hTbU7JvpCkOtqB3HPzgPl6bUcMV6oVXM19HNpJV2huJlR2TaBb2acLS+EOEFGbk3nJh2pI95mXVG4zHWwHIRomIKauuUWYlGFWdJ7Rm+xUOUasNZDK6MHuk2N+l1hauMm+Xcbeqz6q1Waqjgfnn1qUdUMtnIhgHYACHsOzz+I+0w7UJlPuz6StqYc1cQR9kGEymDa/U1lCpJMpbRbjysoPpF1h/htM0mry89UpplaJbVtDWpUrzPSLqompWQlgcLja3I8lmqemqjOH5TYnW/Mc0W0yWRT5FiVZvkl0ZRfcnmYsmEJLiCrYKF4ipBzFR+0STEhlXLzjMvJNyVpWADReR8KVCU4ee3jiZWNXEyLdelUinTL5yoVpyJj1pWcY4ewoZFderdOkBNvJaYHbJJdUdgADzgN4s8E8K8Z5BlrF8stM3K/wA2n5fuvM+VxqR5Rn+EfYwwZhzEUnWK7WK9id6muBySl591XZtKGxsekWM8lJVNa+V5a4NAIAve21jy+amB4I1QfwMmZen+2rxYRMOS8u4600tJcUEXTob3PlrFRwlqUrP+1FxsmpBcvMM/QswkPtWIWQnqN41Piz7J+EuKmMlYnfqVYw/V5hAbnHaYso96SNADbbTSJXDb2Y8IcKa9WKnhR2pJNapf0e/LvrKwEkWU5fmoxNdX0zo3OzHO5jW2tsRbn8k5xWWXkanz09TfYLxMKOpcqiYxYpqoOS4ylDCl6kkbDeNG46cIuHNC9kB6rYepVHlpxmmS7khUmnAH5h1QBUD11j0BgH2ecJ4F4e1vBbQmqtQq+6t2canNTdV/DfaxOkZw37BeCFUmdpdRxHiuo015tTcjJvzCi1TwTe6E8yIkHFYTMXB5aA/NoPENN/S3NK2oZffmgbEmNsO07gfwXw1XsHvY6xJXJNLtGpPa9myFgaqc6wN06TrWFfa84bu1XCFGwHP1JBamKbR5gLS631cAOhj0bi72VsJ4xwNhfD0zOVaRewcgIpFYlHCmZaHrvFNQPYvwdh7E+H8Rs1nE03X6FNGZVUJuZU4udV91d9k+UJFiVKGvuT3s+lifFe3OwG3K90ImZZB/BKsSNE9oT2jKnWyoU6TYUqYCDZS27ahPmYAKiiWxvwOxHX8G8IsOUbBbjDrjNZqc4DPLUDq4jXxX5R6do3s34VpvE/EONUP1JyZxOwtmo01Th93cSoWJtteAxn2FcCMqmpZ2vYqfoTxWtiiqmVCXllqv3gNja8CMTpeNxMxBszkfuixGhGvQnRLHMwG5Rx7KtWQ97OmDZiqTzabsdmh2beCSrewuTrGxtAe8M2soEkjodLj8Y8kcbfZ6fluGPDrh/wAPG67U0U6uofVUUvKaTJsJVdXa9dNo9Qyzgk2paWbWpaZRltpKydVFIAv+EUlfHHI4zRu8ZcbdBfT3TUjmg3HNeTPZ/wAK4e4o8feKj/FqWlKvWpOcLUrK1FVw0xfdCTytA1wbw/R6R7QvG+l4NaZ+i5agPpl0t2UAcpukHmI9AcSPZdwlxOxejEYn6xhquTFmpuZpLpb97T0Vba/WMf8AZmw5ScL+2BxCw5RA6qn02ldjlfJUp2/iKidydYum1jJIZ3sefAO7yFiNk+x7SFB9kyekZL2QsbiamZSWaYXNIcC1pSAo30tGMtMNn/6bMygNNhJxanuhIAsXd7R6pn/YYwA/X52fZqGIpWlVF1T0xQWJhSZZbityRsfSJy/ZLwkeC83wz9/qn0FN1MT6nhftULBuEDyiS3FaTjGRrj3pGvItsBv6pDLGF559oGYqNV4t8F8PNUyWrkk1hxl2Ro8492UtNvZR4jtBngTgxxGa9pTDGLV4Mwxw7kJdlxioyVOnkn3xoi18gOp+EbbxL9m3CfFjCFEomJPfGH8OMoapdXllFMywEiw1GpiPwf8AZZw7wsxUjE79exJi2vMNFmVmavMqUiWQRY5UHnaIz8ViFLlYbEBwtYm9yddwPcXCQzsLLBYB7InCDCOPuInGicxtRJariUxGtmWaf8DaSom9tr+cOz71crXtZ4jkqLg2j4tOG6aiTpdDqboQzKywGim0q0Jj03wp4G0Dg7U8VzuGJiffdxfPe+zwmVXCHP6vlFfxa9m/C3Fusytdmp2q4bxJKI7NFXpDpbdWj7qrbwLsUjfVPc4nKW2G+hsL6Ag8uSETNzm6x3h1wrxjgWU4yz2I6bSsN0Gu0Fx1mhyM2l0Sz9jrlB7ukBeBsfYawH7DtDlceUBzFDdarzsvK0lDmQPuldk5ldLx6e4ceznhbh3h/EVPlqhW6xN4qYUxVKnUJhS3XEEWsm+0Ukt7IWCEcIX+Hk3NVScpRnTOS02tRD8m7e90H1jmYpTkkSkkZmnQEaNFut/rcohIy68m8ZMO4gw9iDhXUqtw8wzw9tWmRJO0V8GYdbNu45bcW5x+jjJSFBTyQpJSM6SNFApsRHndXsM4Nm5elrrGJsWVap0mabfl6hOzKllKUEENhJ2Gkeii2CpCEm/dCbnnYbxGxWuhqY42sNy3NfQjc3G5J+qaneHBtuS8VtYvR7K/ErjFQQ2ZemVynqn6IlAsnt3Lg2+cbl7HnDb8nvA2mvTzKW6vidaqlPrtZSi4cwzfOML4zz9F9pX2iMH4UwOxNTCsPvZcQz5ZKUBDas2S/MXFo9rs9lLtNS8okIl5ZtLTKBslIFhDmKzWpo2kWe+xd/8AiLD33TkjgGi+5XkD23aG1iHilwmpc4kmVqDqW3wk2KkZtReIXtB4AoHCf2huCi+HFKl6FNTU8GZhcqcpfRf7Z5x6L4ncDqFxVxRhSu16bnZeZwi92kqhjwunoryhHFTgpQ+KeMsK4lrsxOS1QwhMdtIplz3Vq6KgqXFY2MgjcTla14cOVze3qubMAAFh9UxrK8UuNWN5ThZwuw5iGo0IFiuVuvuAovYXCEnb1ge9iFmj1DAfFqlcQJWR/kxIVVb81KOqCpWXCblQR5aaRqGM/Y9wnijHs9iqn17E+F5isEGrylHmVNtz4G4Vba/OE0D2OcGYawbi/C1Fq+IJei4xdSubaDxzM2GwPO/OHHVlJ8MYmOIuGcjcWOut7dbWsldNFksPJZJw64V4Y9orienGNPwxSsJ8OMMzQZo7TeVDtZeQdHCD9m8FNFW1Tv8A6gWKpyYHZyshhVTiwjdLaU7D4CL2jewnhCkGQEpjHHSWac6hxhhE6pLacpuAE8gY1SW4BUBvjJ+Uf3yfVV3Kb9HvyiiSy+zlsc3mRB1GKU+Z4bIS0sLQCDptzJ1vzKR0wJ02Xk6Qq0pxTwhjGtcMuCuHVYYcMwicr9YmR2ylp8S03Nwekb57Ccy6/wCzhRhMuuOplZ11tpS1ZilIJsAegiIn2GMFy9YqC5bEWKJXD1SeW+9h1iYUmWLitz6RqXCHhPS+CuCk4Yw1NzU5IImlPoXMDvIzfZ9BEfEcQppqYxREnUEXvoALakk6+miSR7ctgsF9vzDVFNKwTVzTZZVWqFfZlZib+24yVWKD5GKD2zKGxgukcOcJ4JpcpTcPV+otrnJFlXYszr2VJSh1WmhPWPQ3Gbg1SONFOo0niabnJRuhz6J1hUtupaTcA+UWPErhlh3i5hU4fxvJqmpEBJZcQcrsutIAC0K5HSEpMRbDHTtcSQwuuPXb2SMqQ2wK8xVXg/xGqPEjAdcp+BcH8OncP1BoKm6fPJR78xzRYHvkj1gj4YuFHtk8bsl8qqMSB55dfxg6wJ7I2GMKYnptbq2IcVYrmKIsLpctVJtRZlSNjlO9oMaHwIoFB4s4l4gSs7UVVXFMmZWdlVE9iEkWzJ84V+JR5XsJvdhA0O+YHmSeSIytKwP2KKbhd7gXxPHEWWkV4aTX3np9EwgdmQm5ufO9oTwb4SYc47cUmsdSuFaXhXh/h10Jocl3Uu1R9B7rywdQAY1yR9kzCNP4UVzAMrVq03SsQVT6Rm30qIdz3vk/sxQyXsTYWpz8m5JY1xuyJJ9DrbKJpSW7pNwMvSHpcTpnyVEjZHNc86aGwFgCbX3O3kE5xm2JB1XoqYBSTcAeQ5eUVk0CptaQpSM6SMw3TfmImq0bQjMpQbQlAUrc2FrnziK8jeM3Hoq5+qzSmTz+Cpyqy9SaTNKV35eYcsStROm/KKNNaqEzPqnVzbyJtRJK0nup9BtBti7A8xiWfYflJtmXDbWRSHE7m+4MUf5PK1TyjOwiaQs2C2FZviRGrpaijLc73DORrf8AlZiqpaovytByg6K0aarWNqVJmWmGZREjND3wtjKp232tI0GWWkpylSSgC2dRAAA5mKrCWHXsPSEx9I5A9NKzdmnXIOkUnFOh/wAosAYgkG596mOqklutzLS8mUpHhJ6GM/O+OSXhtIDL6EDr+a0FJE5rGl+53Xlr2nOIcjj3ijISeFckwigNdg9No/pFDfXpAogq+1vbWALAU03J+8KnFJ7YuqbJOuaxOt4OBNtK8C029Yh49PAJ208R0jFr9eq2bIDEwNUlDhB0JvDvbKKdDEFMwj7w+cLEylWxF/WKO4SEKaHyBof+sfKmVKVe59IgF5JN77R3tRfQiBSgKemZUDpfWHUzJJF4rQ5bY3hxt0jc3845ForZEyQLEAw4Jsc0iKtL5MOoczbwlz1XWCMMBPB3ESOzR30oJv0jXaWsvTKENAZ1WzLP2B1EZJw2UsVGZS02FFTJusjVIvGn0lqYmH0tSoUVrtsN/Mx5p2kc52KWtfQLXYW1oob7brR6Z2CWFJl79k2O+s7uGIk4tx1RCRYWuT9wQ+20JCQS0bENjMsgbnpFVOTdkhClAdoc7qrcukXIFmAFVAF3khLlpRJ+ueulB0bB5nrH03J5m7NiwP8A7vHZRwzrgdXo0g5UIHPzi0LYKbKt5m0EGAt0Sue5rtUKzNKZbZzzSSUJ1CB9o+cAeMD72wpJbCEbJSB4Y1eoNJykLAJt3RGX19P0hU+waFwFd6w3iBUsDW2CsKR5cbnks3rcmmoy0upaSHmRkULeIRVIod2nE5dMtxGjzVDCn8pGxsSBFNU6eUKcShNggZbgbiK58XMqzZNpYLO35dKmPrB3k91X+MUdTbcdR2UoFFQHjtuOkGy6Ipx1ZcvkT5Qr6AGgyabjTaIzoSVKEoCxqs4Umak2DMFa0o8I6QPjCBl1kLRqOcegnKKU3s2FKA103EClVkJcha0py5T3k21T5xCqYSxt7qRDLmNlmDNDKUKLYCgNxzh1qVUyDYZ0cwYI56TDThKLJO6VD7QiseFzyCj8jFO4nMrEWsqdxgLJKRax08ozTiaw4hKSgktq1UnzjU3CO0soBKhv5xn/ABJlVKaQ82CeSxaLPC5LVDbqFXNvCbLLWAoWtf16x6L4MYrXUqR9Hz7mZ+UNkKUdVJjApeWLjlkJJJ0sBzgwolJq9PdZmqddpxBumxtf1jZRV7aaQOJWakozMywXq6gOZZ9k/dVe/SOYn4iKl6t2SV3bPd33gCpePksUTNNpLM6U5V32vbcRmeIsYFyaU4HM1j15xV43ibqidrYDoFY4Th7YYnOl3W1OYpampstKWCu1yLw772l4aG5EeeKbi91qcKwsqcd0OvKNSwpW3Kk+lJ8IRtD2GVT2VLA/mhrqdroHlvJG/aCxtDfa3PlEcqN/+kcSs5teUbonRY9SXTbTnDKldY+K8x1MNPODLrpDaNNvrsk2NxAZW3rB06nQwVPOXSbaaawIVkZm3T1vFLi33VcYX95D9Lq8w5mCGSoJNrxbprDiE/WMq08oi4eaCGFbb9IugkHkCPSKJ5uruMWCq1V9F+8hQPpHRXZUmywLeYiW/KpVe7aflEJyRaO7SflA2BR3IKkCp09e5bjpfpzgNi3r5xWOU5k3u3ER+kMnwlafjCcIFLxHBWL0vJOK7hA9FRVzWDqbUlku5rnzhlVD1uh1xPxh5mlTKf0Uwr4mDIDNimtXnvBQZjhZS3EHKVAxFpGA0UKZW5IuAFfOLt9ipsp7jwV8YrHJirtL0SlUOMlkItmTT4YgQQ3VSqpTalNSq2WXE3ULZjAI7wtrGZS0ONrK9SLwZisVNsd+XvbpC04pmEaPSywRzAjmySs8KCSKGbx3XcI0+eoNF91nGLuA3BTFTOCbTU1POSrgTrawi7bxYknvtuJ8rRJbxFLL8ZUPUbQ26R+5UxsgDAxp0CpaU+tLizMtrQCeaYDOILpccX2ba8gG+WNTTWJJw+JsnzFojzwp862QpLCgetofjxAtABaqiTB2ueXh+qBgo/SJ1/pv8Isqk7ZBHMxVZv8AKK9f6aLCe1veLGj8TlDrD3GpOH13fc25XgpY1VqbG2sCuHhd1w8r9YKmEoUNDr0vCTmzylpx9mF10Z7jqLXigdpzNPdKmUgKUbk84JCgJHW8VFXHeFoiyOvoneGCblT6VNd3vGB7iBOCbmWZdJBKBcxYSjpbQSDsL+sUlOos5iCpPPzIU0wFbq5jyiM5tu8mK573RCJguSqGSoExVHw1JoJH2ldI0WhYQbo8kciQp211Ki8otJYkWQiXSBpvzMU2LMWOUmbElLpASpN1qjrmRhaFHjwpkUZMmrld4clG3JkOO2sja+0TsZ0Rh6mPTtOfYD7KCS3m3jHqxjapS7CmpAKCTupO8As5X60+peaYmdb3GY2tDdJh12kPsq+ERxtLJGEqPV592sTzy5o95tRFukVslaXmFtq2WNIgpfflp8l5KilZ1V5xKfWO3bUi/nGkEWRuQbWVW9up0Ux1KkS6iPCBBlwqrjDThZeWEqB7t+cA1UdWqWSzLhSs2qrCO4e95p8606ll2yTqbRHqacTUzmk6o4oXb2XrJimoqkj2jL7Og8Oa0FOAWPo+WW05lzKVsDePNz1anX2UCnqfSsj7JIi9wu5ikzLam5xxlrMM+dXKMxSQGmk4txorehgBkAynVeh65RZOog9s2MxGi0iADEOFpmmpUuWSXmfLcRoFOrzFVlZeTZbKpgJAU4TuYtG6S6sFDobKDuCd4t/h6bEIs9rHqplfh5iflWGUV0y9Xl1i4s4AY2uWXmQCdiIGMVcPwxNMTlOyp+tGdCfWDmUpsohhrtXjmyi8LhcLqd0kbztZRaeGRtwQoTZSkHLYax8p3S43iz7Knt/eVHxmZNGiGSr1EXAkYBupghf0VSVFeySTHezeI7qD6xaGfbH6OXHxEJXPuFPdQhMdxmhLwHlVZkXlJJCNYgTNKfUCSmLtc26QcygIgzEwde8SYTjX5JRT9ShWeoTxUT3dfOKlykPA/pLeV4MHjmBJP4xAWwCdQPImI8jyU82Jo2Q+zT3mtA6oQ+mlh1V31qUeUWbjIFiNescAAMMEEp4ABRE0plJ0Tcw6imtA+ARKRa+sOggHyjmtK4kJUmylsjKkDzi8lXVIToYqGVpSbq3iWl3mk/jD7E26ytETKibFRAh9LtzcqMVKZkXF4kNvjfNf4xIaU0R0VqlwDcw6lwCK1Eyn7w+cLM62kXKxb1hUKnl7ppEWc+vl3Wz/AEiCmIy6i0D4x84juVhgXGcXHnBMeWuBB1CQtumsP8YOInCiXTK0SpkU0Luhh5OdsDoOYvHsLhF7QWGOItCl5hdSladU0IyzsrNuBspWNym+4jxpNVGWmGyheVSVbg7GKKdkac9YmXQpY0SpJsR6Ruh2qpZ6YNqo7PH3m219eqqanBopjcaL9GlcWcIonRKP4lp3vF7BHbg/jeCOXrdMmm0raqUksKFwe1AvH5ST2H25l9K5TtJbKdSVEkxdSc5WJdCGxV58pbFkjtToIZmx3B8gLC+/oFCPZqUnuuHzX6lNvSrgu3NyivR0f4woFu/cfYP/ADRH5ly+Iq5Lj6usVFNujxiScYYmT+hrdRSP+KYiHtDhw2L/AGH7oP7Yqb+IfVfpcLjZbR9FiFgKO5T+tH5ooxvixNrV+qD0dMS2se4vH/iOrj/nGB/uGg5ZvYfuu/tip/GPr+y/SPs7bkfrQlTRPT5x+cqeIGL9v5SVb9sYWniHjBB0xJVfi8YT+v0fLN7D9139s1X4h9V+iRaV0hxLR5jX1j882uJeM0jTEdT+Lhh0cTMaE/6R1Ef3zBf1+k8/b+UP9r1f4m/X9l+haWlb2/GOlonkY/PpvihjMf8AiOofrGHFcUMZnQ4jqHwUYE49Seft/KUdmar8Q+v7L38UqH2THF5wNjH5/L4l4zO2JKj+uYR+U3Gg/wDEdR0/rmOGPUfn7fyu/tiq/E36/svf5LliBnAO4CiL+sMKbXfRJEeCUcTcZ88Q1H9cwr8puMxtiGf/AFjBDH6Mdfb+V39rVR++PqvfDalIUL3vf5Qmn4dpMjWX6xJ0qSYq02jI/PNos66OijzjwMriXjLNf+UM/wDrGOo4n40G2Iqj8FmOOP0h2v8A780rezFUB42/X9l+hbgzgDKYjLlz90x4Eb4oY1vc4jqH6xhz8p2MlHXEdQ/XMCMdpR19v5SHs1Un7w+v7L3olopPhMOhCraJV8o8CjibjG/+kVQ/XMOI4o4ySb/yjqGnVRgjjtL5+38pB2ZqvxD6/sveqkKI8KvlDaWla90x4RVxWxmvfEU8PQmEHihjIq/0jn/1jHDHqXz9v5S/2zU/iH1XvTKroYUAoW7pjwQeJ+MuWI5/9Yxz8qeNUnTEM/8ArGE/rtKevt/K7+2ank4fX9l77uo6ZTHAlea+U3jwJ+VfGpOmIZ4fEx0cVMZnxYhnv1jHDHKPz9v5XHszV/iH1/Ze7JahyNOmHnqdTZCTemNXnZdhKFuf2iBeJPZqTyjwUviljL/8gnz6LMcVxPxiQP8A7hn/ANYwf9dpOpPy/lCezVWfvN9z+y97hStrGPlBR+yY8BK4m4zvf+UNQ/WMfDihjQDu4hn/ANYwn9co/P2/lcezVWB4m/X9l75U0beEwhLKr3KSI8FJ4p40/wDyGe+Zjo4pYzuL4gnhr1ML/XaPqfb+UP8AbNX+Jvuf2Xv1ttVh3TDyW1DkbR4B/KpjUf8AiGe+ZhY4q41t/pFPD4mAON0h6+38ov7bqxzH1/Ze+nAu3hVaGClR3B+UeCFcVMakH/7jqH6xhlXFHGw1/lHUD/eMKMcpRvf2Xf23Vn7w+v7L3ypJ6GEhk38MeAneKONF74iqH6xhv8peMr/6RVE/3zDgxul8/b+UH9t1X4h9V+hCEKAGn4w8EaG9vnH55HiPjAm5xDU/2piK9xAxe5viGrW8njDZxyl8/b+UY7OVP4h9V+iC733T+tCS7YWK2/isR+c7uM8VugheIqwL/wD8gwycR4lWLnEFYP8A/oVCjHKTnm9h+6L+2qn8Y+q/R0LbI/Ss/FwQ068yN5iWHq6n/GPznTW8QKN11yqn/wD0GEKqFZcP1lYqh63mDBDHaLnm9h+649maj8Y+q/RUTEoDdc3JgDmXk/4w61Wqayr/ADnIJP8A/ZT/AIx+cahUnkFLlUqZBGt3zEI0p8HWdqBP/wDYVrDpxrD3bl3sP3XDs3MPvj6r2Lxx9qakcLZynStKbYrzzyiZ5uXcClMp5WPWPOnFD2oKrxaSxRqJJu0OhPOj3hClfWvX+8RyjPxhuWLhddSXHD4lOG6j6w+1SmmDZptCB1A3h93aagiiAgh7w5k8/TZWVPgMbHBztSFFFEZpcy4xLOh9pCu46OcTENlI0URElDASnbTyh0NJ52jHzVJkkLzuVbcIgAFQD2gOhV845ndSbBahE1SR0hpxrMNOUNiXXdIYhbZMe9Po2cMOJqj6dyDHwYB3hfYJA8I9YLjuGzkPw7TyXRWX+YFoWmvuJPeR8oZLNwLCEGVzDU2hRVyDmhNJGeSsG8Ri4uk2icziNk7giKFMqlJ3uYfQ0kHQDWC+OlAQ/AxlbhwLkncTVOeVJpultvKd9SY9J0vDDNDlsqLLmFC7jtucZZ7JksDhipzBlw2EPBKHfvm0bVNmySB8dYgvp2SymoeO8dB8lHlmcz/p2nuhDNXe7JpQFglJv/aMBlUqSG0OWOYgH4nmYKa0C4frDoLlKeSR5xltbnVN9opNzdRPrFbVPLVYUjA5HOEZ0zNPS4tQBSrKAekEbb4eeUlB7jWl+pjGJDFSZYyi2XcqWnMriCesaVIVdCXErQoZFpFtd/OEgqAQG9EtRTuDi7qrKfTeVdXe3dNz0MAFOlgucfmFDwXymDBU6JuVmmlHvanfaBGZmk0uluKUe+tRtrvATuBIdyCdp2uDS3mUy6jPMEJtdNytUUk80l0ktDMOR6wtU2twNM5rdr33DeFmfYaC3P8AZp0F9BEMvDgpgaWlD1SlBKybaO7ndXdXpE+Wkm1toCkjvJsD1EQqVKLqz656eUUs5vq0X3EFcvIKcLakoyoGiEwEbSTdOyODRlug2bliyLgeAnTyjO8Sytpp1aE2DnLqI26o0kIKioXSVaHzgOxBh5qZbUkp21BER62jfLHYJ2kq2xvuVhdRUUtlH+zJKT5RQzDmYGxFtwR1jQK7ht5h+yk5kEnvjpAHVpT6Pm1svAg7pI+0IzJp5GHvhaASscO6VWl8vWChZbZteBrESUz63mFEWItfzgkbKe3AJv8AdN94EqnMBmdmc2tlbX2MSacEOuE1Lq3VU1GoX0Y4p6aCFO3s2N7QSS6XlEKF03P4xX0xl2bfuTorz8MGkpQ3UMFY75tqOgiTO+R7sx1TUbWNbYKMyEzTCmZoXSsZVXHh9IxvHHa0GsLldVA6tq8o20tAKtYg87wD8UqS2/SmpwNBTsurLmG4HnDmHTAT2dsUFXETEcvJANHcUSkq1UreN84VhpDBcfspwiwT0EYVQJVbzyAkamN6wHS/o+WU6tWZahY67Rd04Lq1luRuq6WzaV2ZaEHJcjVEJPuqjrpEAPi1oQt650jYZysxkb0U1xuW+yq0RXZVpwnK4B5XhkrzC17fGEdmm9yfxjjIV2Rq+ep31ZKVg22gKrY7Np62/ODRZCUKyqO0A9dX9U/1secU+KPLsqtMNaAHWVVR37S5F+cTxMLvobQPUqbyNqDqSnXSLVE82dLxVkK0aVZNzBOijCS7ZVradYiomUHZQ+cOh1CuYhs6JwJanRyAJENKIKStYASN46pQ/wDZht5YcbUhWgULbwmZcos2VraKmFJI5CKGj1Gopn3UzjKg1fukwmoSM0womTfVlvcC8VC5usMmyFBQ8xEtjYXDdQXyThw0Re7UheyjYecdQe0AUlQIOxgAnPpmdIBOX0gqw+iZYlgmbVc+ZhuRrW7FORyOcdQri/IC8NKTc6gH4Q/ewHLzj7N1AMM3T1k220g+JCCfSHewYUO82jzFo6SFJsBbzvCTt6QB1RjQKNMU+VUCeyTbyium6ZKraUUBSdNwdotFrsIhTdksry6XHWFa1C43QKFk1By/N+LeeOkUyQRUVjn20ED0i++nutmx8ovaXRzis9V6saExQu6lZ6qgjlzYixgZbl5un3CEAgm5vE2VrCkCz7ZHnDc7TmJR07hlAREpzNoIqqqbAXhxmptOjU5fWINZd94KEMEH7yhENwuQpd9E7KDMmL2RGWXVyI5CBOX7VjZy484u6fPqIKXAbK6QL2EpWOAKIpCZ7oBNrxn+PEqerilC+iNI0ajrlUpGZsqVzvFVjrD0vMyi6g1dtxpOo6wjWlhRuIeFlbSSnRRv6x0y7az4RrtDYmkhffSbekOpm2iRqfPSCIO6AMbsuIpMu4oZ2UKPmItJagyJAJlmeW4hhubZFgFfhE5icaJGZxIIiNM6TkSpMUUY3AUxiiSY8Muz8hDwkpbVLbLYI5ZRDTdRZTs4LeUSWZ5kquFa+kQJeJzupzBHtYLjMklK+6gJ8wIvZBpemh084hsTDa1ApSVWPIbxey00F5ezl7edor5XutqpUUTRsijCSFImgbkG2vlBs3fmtRv5wP4elmmpNDyRdbg1uIt+35C8abC4nR0wvz1VLXPa+Y25aKwskiy7nyJj7M2OUQw+DuYSZgA7xPtrdQwpZfSnZIhBmFdAIgrmQFaw2ubv/CDXXU8vq66Q2uaI+1EEzQIsSBEd6ebQi5WPW8EAkuFPcmQL66cxEVx+51MVTtWZRu6k69YhuYgYTfKon0hcwHNcrtTpXv8AKGyoEa8oHHMUt3slCz00hhzErivA2q/K8NOljbuUTWPOwRK44E7axFcdtzA6wNuVeccByi0RnHp57dRsekNmqiCIU8hRSZtKR3lC3rHwqrKDYuJ+cB/u00pVlrWQfOHE0patTnPx2hs1zOQRtpX80WGuy7Y8aYScSNZbNkqiiYoitykxZy9GVyTy2tAfHX2Tgo+pUkYhUoHKhV4W3WJlwaJsIdYoyhsn8N4mt0tf3dfSF+Leea74Rqge9zjn2ikwr85c8ThIi4bpasuo/CHkUtVwbfhB8YnmiFMByVOJdZGpPzjokyVfwMECKYonw7+UPJpJOwv8IHi6oxCEOokz0hz6PNttIIkUw/d19IkIphA0T+EdxCl4QQsmmm97bw8inHrpBP8ARZP2fwhxFLym4SflC5yk4YQ4mmaC4vDqZApBFoI007nkhQp5P2APOFDkvDCHUyBtsQOQh1Mkb7QQfRyr7CO+4ciL/CFzhJkCofcSRuLQoU/NuQYvPcrDw/hCxKW+xaFDwuyKnEgEp1+cKEkLbaRdolk8x+EOCWSeQv6QeZCWqkTIHcCHE0/MTpaLkS6ALR0MITyjs4XZFTfR9tyLQgyFlaC8XvZp6fhCC3pokQmcLixUnuZJ8McMqU8oulNnpr6Q2pi2415xwckMapFyxHLWE+7G2x+cW65a41IHwhsy9t7/AAhc4XCMquDBA2jobN9RFl2CUjSPi0ANQLDyhc6ThqvDVhoLx0NG3h1idZI2G++kfW02sI7iJeEoQZVzAjpYHO8TCyT/APEfCXNtjCZ13DUQtAWhCmx0MThKncCOlhVtQB8I7OkyKsLetwDCgys7J+cTCyRpfSOmXVbcmFzLsiiBgg62+EcLJJ5ROTK33vChKJ57R2dJkVaWraJN4aLawbW0i2Eqgco+LaE6EQvEXGK6qwy4dhCky7itDYCLAlCN0n5QkuoHKx9I7iLuGFFVLqFuZjnYK6W6xKU6kW53jocSY7OV2UKH7uQPWOGWWVakfCJoWOQhaUZzawHwji5dkVYZEnmBChKgbxaplSrl+EfGSI3EdxSlEQKqFy+v8YbUzbTWLkyIAuRp1jgk8x7qD8oQyLuEFTdgVbJJhaGCARli6EgroPlDqZGw71vlAGZGIVRplyPs6Q4iUUrW20XXuSeW8cEtYaDT0heMu4IVSJZQTtCVyyjuIuBLG2x+UfCUzA3H4QvGQ8BURljbUxxMqDvF37iPhHfc0p3Av6QvGCTgqnEsOYvHFy46RbKl20jfeI7jY58oISXQ8JVS2ArS3pDZlzmtY+sWoaAvpCC1Y7fhBcQpDEFWGXUOW0c7Ek2izLebYbeUJ92EJxChMQVYWzexEJ7Ik6RZmU1/6QpMrk5bx3FXcFVfYKGgBjqWlpO14tCyE9ISGcxhOMi4IXqr2V1KTw8eSoG3vOmsaxOKCUG5sOvWMm9mBsM4EmiVlRM0O6R4dI1ObBUCpW+6R0EWETrwgrM1TbVLghSsKL3aBVxcXI/qxmuJ222pZu2inFmw6CNIra0oZWQe+4bD0jMMXntAP6gIEU1XzVvRjZZpX7sqWuWUUqvt6QU4Dx39JyQZdX+cMEIWCdR0MCdbWVhQtqrUabQBsVZ3DWIWp1q4ZdOV9PUdYojIWPur7hiRlivUkjVFJmUu7pX3Vi/4xUYuWUrQ0LlJVceYgeouI0TjKFNrzJUkG/WLGqT4npZNzd1jbzETHyB0dlCbGWyAqvnZrsllKTqU2J6RR1SqhiTcBPdOmnOHJmYK1KIOv8IF65NF2zbdzmUEiw53iG9+qmxsutX4d0Z2rSjc5OJs2NGW+Q840RNIS3Y2Gm5/whWBqEadhuQQ+AlwshStNh0i5nWghq97X0CbRfwU4bGCVn56kulICEpyTQUrzi4PKA2sSac5QjTTuK/hB5U7MNuZxbKnSAStzF0kp8TWoNvwgJGhPQklAVYkShaipIAVfTleM/xNhtNRBJBS6g90i3yjTavNC61gBTTw7ybbGBaZTnJB1B5xWTRMcCCFZwyObqsgq1Bdp47e3dT4rfZjLm2nK3WH8tw2HNVR6DxgWZalzAmLJQ4jLqOfWMow7SkJWG2k3Ga6lW3ivZStY42VgZ3OaiLC2G2UoQezF+h5wbfQqQwezTqIh0uWCEN9mLKTtpvBKyoLSCdD/GJoibayiGRwN1n1apZl050pu2ToeaT5wI1ltEzT5ll4ZkrbNx59Y0/F7RYklutC6F6OJt+MZc88lxWXfN3YpJ4+HNorSB+ePVAGG5EJmMiL3zWJHIRsFJqMtJSqGUK8I1J5xRUvDrFOQopSCtw3KrRNXJoA0TaNTQ/Yu4h3KpapplGUGwCI26uwq1nEw8mdacOi0k+RgPclNe4SI6lhabWWoRbtrhzCrjRHkUZdsle6h8I+KwdzAeH32zlQ6o2j5VSm0DRV7QQrGEoPg3opeeypVrygCrlTQ322e+m5iW9XZsDUAxR1NRfYeUsC6kknSIdbIx9iFKpGPjuCqyVxNJLOQuN5hyJtaLVmal5hOZASoeRjGZuQK6sBdQC3NbRrtClJKQkUJzd4p1JMNtpM/hcjFYWnVqmhLKiLCwPnDyWWlCwWR6GKmoTDbZJYWLeUZ5XcYz1Nm3RLunKnYQkmHyW0N0rcSjvZwstUcZUm/ZvGI6/ek3s4FCMSY4xVP3zslJCxe0FUrxPK0D3lhQPOxgTh87dwuGJU7tAUdq96WbqQlUNKQsH6xkgc7QOynEuSOjqFpvFkjH1McSLPgX6ww6CVp8KebPC77ynEoTa6FJ+ESG320jRdvXnDDWK6bMABMwwq/UxKROyL50WweliNIaII3CcBadiE4iYQU/pEwtEwLXzA+hhlxuWWO7lPoYaMiwpPdKh8YC4TmqmF8HY3htb19AYh+4AHuPKHSOiTdFyh0H1hdElynXHTbu6mGHln3debpDS2ptGgCFCGnTMlpYU1e45QdtEJchMECrOdRMf4QeNuPFCcoTtGepv9KuG/9P8A4QfsrORPW0WkVw4qmksWtUWYYcWSSrfyivek1XuVH5ReKGYXG/S+8R3UpUNdocc0lC0hVCZbXW5iQ3L62Gt9okhkX0P/AFh1pABvvfnDBZZPB10y3JJzXVv0izlJJNwQISwhKljNrFkynLtb0gcqIKwkGg2Bl06R3FA7TDk4nmEEx80sJA6GFT5E1T32lEHO2RCFqLNosNbAUO9y8okNyyVW0HkbRHeBlpp1tehQsiJcu6U6ix6iGnghOx2KkNyKU8h6WiQzJtlY7u0fNO5wDeH0ryqBToYiOupTQE+zJIKh3RFtKyDYOoGvlEGWf6AA84s5V4FYzGxiDPmspsQarWnyiDewFxvpF5KsJKQQBfpFPJm6+6bA7wQySU203A1EU8pJNlPjtZX8lMNsSjaCoJyiPnKswhXedR84HXJNbiyVLXY8rwpNMFvCTGsirmNja22wWefSPc8lXa8Qy6Nlg+ghleIkqH1aFqiE1TeSUC/WJDdNWD4LDnCurzyCJtEOaYerkwo9xq3rDCp+ecHdIQD0i4RSirxIv0iS1R1EaIt/GG3Vsp2KdFJGOSGuznXvE4rWOfRrruji1mC1uiOXGVJMT2qAo7o+MNmokO5RiFg5IHRRQBqCfOHG6CknVP4QeN4cJUO4bROaw1ltmTAZnlEGtCz1GH0ZhoPlD6cOJ+yknzIjSGMPIvcIB9YsJXC7kwQG2gbw24utqjaG8llyMNZrZUfhD6MN/wBT10jZJXhzNPJHdQj1MXMnwmLgu64kGGHTtHNGGhYOnDAuO7p6RKYwySR3CfQR6HkuE8o3+lIVFvL8OpCWtlbQSOdob+IHJddq85y2E3F2CGVEH+rFxLYFmnAMksv1yx6FYwvJy9sraNP6sTUUyXRs2PgIH4h3IJMzF54Rgmba8UurzOWFjCjyd2VD1Eeh1Uxpe6B5XENKo8soHM2j0tEhlU8bhNuLSsB+gFItmbOnK0KTRsu6PwjdXaBJKQbpRfraK1/D0kfEE+lofFZ5IQ2/NZAKQNwkQr6KsL6ekag9QJEDugRXvUCWPgh1tSDyRcNZ99HWPKxhwSOUchBc/QWgTYxEdolgcuvxhwStK7Kh0SabbnXyhaZYC2/yi1XSVp2vDZkFpTc6+UOBwPNCQoPuybbj4RxUukdYl9hrrvHQzfeCQqF7smFJlkG53tE5MuOQvDglyB4RCXC6yrgwCPDf4R33W40AHrFj2ZHLSOhKSOsdcLrKuEiSN7Qr3IW1iwy2hJSVcrwuZJZQRJA62jvug6CJgSeUKy+UddLZQvdwb3H4Rz3VPSxiWRY6RwpJ23hLpbKJ7ijfUx8ZJu2qYlhJAjhQTfUQuZdlUFUq2Psi8NOSyMuiR8onrQm+4vDS8twBdXpHZl1lVrlidhDRlFHQiLgNkjRB+McEu4o6JCfWC4lkgYqlMiQf+kOe5HoItkyaidVfKHESCb6qJhOMEuQKnEqryvHxYWnSw9Yu0yQBN477kD0geMuyqiLahyhpSTzTpBAqRHO0MmQsTcaRwlCUtVEQR9nX0jhK+SRF2ZADcCOe4A8hBCYIeGVRELvawhC0uA3AvF4qnm+ghBpyzpaF4oSZCqPI70FoSoqR4rRcuUpxXlEZykrB72oghK1CWFVS1i9wb+sNLVfz87Rbmk23jiqX3dh6QfFag4ZVGSSdPjpHCojTnFx9HoGqiIQuVaSdVC8dxLruGVWtvqB2vEtmZUCO58IWUNIMdC0ch84LOkyKUzMnmNIle8JUNREFMyANAI6ZhSuUCUQVm3kWNN/OJCJVBHdPpFGlxzNoDEpp55PW0NuBTjSFae5X84X7ogb6xERPO2sYWJkqPeVaGi1ycuFJ92aT0hKmW09PSEJKVnxw6llBI70J81yaIbSNoZWQfCiLBMok7aiPlSwBsbQtwhIVSpt1ZskAR97kojvG8XIlgnxXB8xCxKF02bQtZGpCRtHcQpLBUBp5PrCTTRbX/wCIIfoyYWkKRLulJ2VliQxhWpTR7kssX2vzgw9/RB3BuULppyRCTJtp3GsFicG1NwLIlldze8KTgGqLAJZAvzJhftDyScSIfeCDlMNpGiR8oZU0kfZEaEOGk2W0kuNdoo94E+GHjwvIsFTKfPyjhHN0QGog/Es1DKleEaQsyZVGmDhi3kGWZN+ekSBw1lezAD6rjxQfCmPJB8TB1WVe4A6EQoSISdBfWNXHDqRDyT2i8oG3UxIc4fUxx7MgrQm1soghTyoDWQhHXs4SwRgN881zeukaNUllCCi4ufEYHuFVFZoGFewlCpTa3cwv1i9nFN95Tir6+EHxGLVgLYGtO6zc7g+pe4bXQnVmy7e/PbTlGbYkWHErN9CopjRq3NlpDy7gL8IHrGY4hSWXS0o6FJPxipqiFbUYWfzyMwdSrRSDdMAeIpEOtuAC5XsLbwe1lzs3krG53ED8jT112vyMlLJzLfmUkD+qDqYoagE6BX8Lrd4pnC0zMUiUbZmSsFA1ChqmChnEeZ1tSSMyTlUOogi4l4XYlimYl2uzWhAS4pA7ptzjKXS5LuBSDmAPdUD++Oex8ZylIx7JRmCLpydQVOZFWAOvUQnB0kmt4zpUutOf64LyW0IEDKqgXNXCCT57xqHs60n6RxjNzq050SDFkK6EwUERlma3zQzyCKFzvJej5VsITl0ypsLAbRFngkPpsAcupiYEFKN7W38zFZOvBPam9souTGtebCyyLdXIcxAA5nGyVeHyjNqrMBxlZGjjRyqg6xBUUtdivQ9oCCIzKuv3deUyqxO/mOsV0zla07dELVGZIWu2gN7pMVRfBOm3MQ5PzKisodINtjA9V6qKZKvzK1ABlBVqdjFc86qyaNEI8VKujtZeQbUkrV3lW5CKuiMpl2hawUsD5QBv19yt19czMrzqdc010A6QbUucSZlKSRYDS/OGSwgp0O0RxIOZGm1aXSdfSLgrBF0+FX74GaZPIcaUnMLJMWbM5cZc2n7ocYeSB2qrca1FTNLIAuFqsqMrDqe2sNbq1jVq5LpqMs4yvQK89jGcrw66xUGkqsUBQJVeIVVA57wQpdNKGtsUSJl/qUE2HdHxhCpdJGl7xNTbKAQCALCFA22AiwDiBZNZQSqwyhI8J+UMqYKFd4fhFypTnIC0RnUKWe9YiDEpSGMKtJCRsPlrESYcOo0+UWa5dJ3MRnJdI8VjaDEg5oeGqV25uTvFbOODsHbEEZTF/MtMhKgDcnTSB6ZpSVBYQ4oBW8JJKLJGxkFZy2wmYriQq1kruTBTNTzLKLBY0iOvDLbEypba1XPPpDczQQpJso3h6KaPmVFdDIL2Co6rXkIUQ2o6wCVuoe9LWTuRrBvOYXWtRgfnMJLzKuL3EWUU8QG6rJoJidlngaQ1M9olXeB0EWCKm6nne3lFpM4SdQ5dKTf90Rzh59J8J9IsBURu5quNNI3koX04UmyhfytHTXkFPeuL9Y7MUJ0G6km/pEB2lOJFikjyh4GM802WyDkpyawkgFDtvjaH0V11H6KYWPRUULlOVksRYGGTKLT4biDMcZTOeQFF7OLqgyB2c45YbAmJzHESrMqBEyVEfejPyl1B0UY+Lz42UTAGjhduAnW1kzeZWoN8Vqi3bOEL8osZbjC4dH5VJtuQYyATj1tbG3OHG6g4n9IgH0hh2FwO+6nm4nOD4lt8txalFkduwtHoYtJfiZSnRqtaPWMGbqSdMwI84lNTzZ1vvyiG/CYuhU2PFpeZC146VJ3/AI3+EG7J+rTfoIzk1BX0i4QgkF7e0Eorj5ACEARHYQ1xunSC5gsiNS9CCfxhp5YAGpvz84G3KlNq+2ADEdb0wvxvKPpBumZZI2F6JlPpTuoW9doR9JstXBcST6wNpYK9VKWr1VDzUqm+g+cMunFtE82F3NEKK4ykgJuo9LRNRXQE2QhRv1gfZlbC6R8onssaeIAc4jOndyUhsI5qzFafV+jbAPPWPhUJtz7SUjpENDYzaL+EPOq7BlxwHMpAvAGZ5R8Jg1IQXimSXK1AvOeF7W4irafAOpt8YXiOszVQPcSDkOiYoUTiye8242roREhsbnN1TBka12iKWH7KuTp6xYNvJV4VXPrAkxUCiwJPxEWDVTTYb/KGHwlPsmCJm3CLWOsWUtMBSRnOvrAgmrkWAzHztE+WqTjgGRDiutkxDlp3EKXFOEbSU72agbi3rBnh3LPK7huQNoyuWM64PqmiAea4JsH0mozFYYBm1l1xQCUNmwEVb6ZoOYlT2TEiwC1dmiqUB3dT+EWDGH1EeEjr5xpVCwKlcsyH32irIM2vODKm8O5EpBeeSo+VokAtTRJWHsYe72qSByixYw0pZFkE/wAY36U4f0tJ1QFHzi5l8IU1gDKwgfDWCzjkEBkA3K89S+D5hwfVsOH+7FxJcPJ51ItLqF9iRHoBulSbKRZCEkbWESW2pdGgA+UFxH8gmjO1YhJcLJ1wjOhKOtxF0xwnKAO2c9QBGs2aTrl0j5Sk8m7whc8803xzyCzmX4Zyrds5UoxZs4FkW0W7FJ9RBcs6aISmGlXtYrTAHNzKXjOQk9gKTWO63l9OUQ1YJ92VeXcKYNFuJQO+6kfGIT9TlW0995KvIQ071TjXvKFjRp5g/VvFQHWHUvVKVIzozecWjtfl7/VIWvpYRHXV339JeUPxEN3Ce15phFfmGv0rR05iHP5VoHjCh5Qh2Vn5n7DbYMRThh55RL7tz0AggGc11grEYsYtqrWFjFLJGhT84pnMJA+FZhhWE3Beyj8IINj6pLBXi8R57hKgB6wwqslX2vxiicw3MI8DqobTQphPidVaHmRsSFXblX08X4xEeqtz4tfWIYpa0+JSiYT7oEjUGHhG1JdPqqN+cMrniRoDHQ2gbphQCVGwt6QVkt1HLzq+RjoQ6roIlhvTQXjqb7W+Mcluovui1Dvqj4U1B8ZuYm5rmPlKShOqhrHLrqIKa0Psg9YV7g1bwJ+UOl9A6whU2gDe0dqusvkSDSRohMKFPaP2RYwyam2nTUwk1UDYR1iuT6qY190Rz6PYSNUpHXziG5VFcrwx7644bEmEyu6pQrIysqB4U2htTEuNkpiKStQ1Nut4+7MEXUuOvbmlTikS4vYAeURnEsnYekPJbaB72vrDqUot3UQQckVatpJvlSr5Q37mtQulKrdIuQkHQJN/SPiySNQYIPKQqhVKOcwqEGWI8QMXxYAGiT5wy4zf7JghIUllSFgDlHVIAAI38osFyqlbIA9YZckyBqoAeUFmXWUM7eUfBYA1hxcqLartDJQ2nckmCuCksnEPJ5m0KMwi2hvEYuNNjb5w2ZxtIIFgYSyRThMdEkx3tVq2TaKxVSCeRhCqsr7IPwhchXZgrUqXfUgR3N95Y9Yo1VF1atAY4Zp5XLSF4ZSZwrsuISdCFAQj3ptJuSPnFKp14p1NoZPaKuSqFEXmuMivzUWxtb5w0uqtp5D5wPq7QHRZPwhSEPKTfIon0guExCZHK3XWANgLxGcqubXKIgiUfVsAkecK9wXbvuADygsjAhzPKU7PrVzsIYVMKX9omHxJNp1N1Q4hlKfCjSOBaNl1nHdQQhSzoCYUZRahok+sWjSbnw26WiS2zm8vWF4i7IVRopzqzqLQ8ilWPeN4LaXhmeqk21LSsq6px3wXQQPnBhS+DFYne196S3KKb2Cz4o4Oe7YIXyRR+NwWVIpiR9k+phYkgnQJjfKTwTkmZe9XWp14ghQQdB5iLWV4TUJhkIWwXVpVcrJ38ocEMpUZ1fTjbVecRJjTS/pFtI4RqU8wXpaTdW2keK1o9Ht4Mo0s2OwkGARzKbxOEqywyG5dtKE20SkAWhxtM77xTLsTbbuNXnFvh/WVtoc9yUEObaxYM8Kau8pPaNobSdzfaN77BJCNLDl5R1csNVZsu9784XgDqm/6i/kFjkrweU3MtKmpjMzfvgaGLVzhNIqXdpbiEC3dJ1jSfdUEXvvtpCkSwtaxv5wohZbZA6tlOt0CyHDWmyikqUlbpG2Y7xMGBqb74HwwkFOyeUF1glKcyScxsBCVN23BghG0ck0aiQ6kodmMKyL6k3lmrp6JjjdAlpVR7NhtJULHTlBEWgk5k3BG8ccCQNEg35QthdBxHW3VCKa0EhKG02H2bQoyoYGoA6GLZVreA39IQtlLoBWjQbDpBZrJNVSlpTtwhJHmY4JRRHeB9IuVMlYGUAD70fOSqlt5Ghlvuo7wgclVJ7kLG5+PSGTIoJNydNot3KeUIJSpWm/nC0yaAhI2Edn1S7BUrckpxVtQnqY4qVs6UWPd53i8UyWzcIJvzhBbbUnRJJO45wWZJa6onZNQ3Csp59I+TJrVZKUnMo2SPOCCSpExUXexY9So7JHnBFTMMy9MeEw8szDiNQT4Qeog48ztQNE1JI1mhOqsqXIfRdJlpcmykIur1MQakteUhOVAG5GpMTXagXSS0LgfaO0Uc/U8qrk89NNokSublsFDja4uuUO1tlSEWdIC3NUi+w6xmeK5kuJLp5XFoP6nNq94deXdZWmwB5RmWMH1IllgA3So3PWKSqItorukBuEB1mbPe1ub6Rf8FpZr6bqNbnVBLFNayNqJ0CiNTAbXXSkFSb2SnMPlFy445hjh7LsFwhyoXmHLbkHlFMJMrw7orox5mZb7ojx1xHdq7L7FGZDjSL53CNDHn+qYqmKfPrU13SVHMgm6VRqTMwiTwQhxAu5M3Kj96MRxUrO8tQ3BN4ccXPNyV0bGsFmjRFtMxlIVAjtFiVeB7yVHun0j1j7KtPQrBE/VE2Pv0yUJVfkI/PB19QCgL3WQ2gf1laCP0/4HYTOCuEeHaasEPKlg+9f7yheLPCYLTlx5BVeMzfYBo5lFNRmUtjKk68tdoG6jOZgtrbtBvfeLuohHZrIVdVtfOBKuTQZdasb5U6kCLGcm+6q6dg2QliCaCpdSQrvNHa+0Z1WZxYUpabgc033glxLNKbedUknXUW5jpAPPz3bJKXu6pOqD1HSKyR11cxNsFUT7gUkqQb6bX3jDOOGKZiWXL0uWUtCXE9o6oG1x0jYK5Npk6fMvKJSlDSlJP3THlyrrqONKiXao/wBpLy5IStItn12EMwC8lzsE7MSGWCqKZWCibSpoLcUDskXtBpJ1aoPELYlnbxCplKbammpWVaSi4BUQNUiNPw5RkAgZbp0+MPva0m9k0wuA1KGqPiSZlXMs4w+gcyUnWDKn1hEyn6pwEncX1i9l6YwuYQyltLjh3GUHL6xIqeC5J1Gbsgw4NltmxBhh8Z5J9sgA1VWioBV0uH09YcpkixPTf55coSNNecVztGflHLB7tUg6E7iFy02ZQi5IvBRNN9Qhe7oij+TlOI7pUPjEZ7DranB7s7lRzzRWCtOAgZj5Q4Ky51Nv3RMMcZ5JkSyDmpiKB3lB1wZQO6RzionaRNNZilBUBtbnFkKocoKibGH0Tyinu6k7XhvgRpwTyXQU6xNZikNOAjyiO5KuggTAUk8r84OgsuKuR66bw6mRZmlAzDYX0vygDTjkUYqDzWbusJSdBoeXOK2bRkvew8411WGZKbUCpq1tynnFTUeGbU2pamHsjZ2TDEsEh21TzKmPmsamXFBwltAJ5kxEdDh8asoPSNGqvCueQv8AMyl5PW9rRRPYNnJJ9SHZZxak6qIFwIb4b2jUIuI12xQcthStsyulohu05xwnOdIMlyKQCFJUm2motCDTErFrQjZLLjHdA5oaFq7xUYd+hGwO6kA/OC9dKSkaA6RBmJVLex0hwTEoTCAhJ+h5gbpbPwivdw20tNlNp+UGa2Ao91KlHyENGRcVfuBIh5tQ4c0y6BpQC/hGXsbpy9Yq5jCDJCg3e3IxpiqYCrvi584acpyDcFIHwh1lbINimH0bHcllC8GC51JPW0RXcHOi/ZJ+JEa39GIGgAI9I59FBV7p/CHxiUg5pg4bGeSxhzB7qUnMCq/4RGXhh5OiUq842s0hBuCj8IacorIHeSLecPDFnhMnCmLFTQX2xqgkRFekHGge5bzjZZmlM3slvOfIaRVTGGUvEkoSAeQESI8Uue8Ew/DLeFW6pRXbOqA0z9IlJZVlFifiImmWOZdh9qH2Za+q9R0ioLjdWgYAFWe7rG5PoRHFS6yO6IuxKosLwr3NBFxeEDiiyqlRLrTvaHUNqGt4tjIJUNUkwtFNFhZA9YW6SyrEqWnwk28hCu3cB0Bi3RIWGiBChTc32b+gggWpCCqYzDoPO/pDL8+6hJ1O3SCBVJzfY26mIk1R12NrBPTeCzsG6EscdlldaqC5edUphtRQT3rDnDTFfTcZ7p9U7QeP4bS6olQF+ekNDB7Cz3m0n1TvDpqobahMinlB0KGpetyxIzdir+0LRJTWpQ6dmz/jF6rBMqf6BF4cbwLK2F2U/jpDDpYD1UgRzeSpkYgZSLNIZt8rRJRiZCAMrmvIJTF9LYIk0gfUoPXSLWWwnJtgZWUD0G0RZJoOikMjmQvK12cmSBKy77p89BBhhJVZZn25lyzYFsqU8onStEabtl0ttYbwT0iTbTYWHnEd0jCe61SGsd94o6oNdqBYQpcw8DbrBRJ4pqLJHZzj2v8AWgRp5aQ0AVJ+cWjRQR3Vp+cWcTGObqFEe54OiM2Mc1dq2WbWbdTBBTeJtTbt2y+0HnvGcMrSLZ1jTlFnKuoWoZc6vQQ4YIiNkHEffda5TuKKFACbbi6a4iyCrb3PQRk0tLyqkXe7QGLSUEk2BZJPrEd9KzkEbZAd1qzGMpWaA7C5PSH1VqZcH1EqTfYqVGfSdUZlgOzCUn0i7lMT2SAViIr6ST7qca+NXzjtVf2Q22et4aEhPOg+8TWUHknlDUviVojvKETG63Lq3KbxGdA8bpwSdElNAQ4B277rnmNokt4flEAfVlR84WiqMEDKpPzhf0ii3iSR1gMgSF70tFNYZHcaTb0hYYQk7AfCIhq6QopJAtyvEJ6sA3yq08obIAShrjurgpaSdQIbW+0kWyj5RQrqTqz3Ao9LxHXOvX7xCfjHWKMR9Srt2cQm4sL+kQn6gEg2ygeZindmSRqsn0iI48Sdj6mCEZKMABWrlSTexyn0iOufCjcAXivuT0jhzAAm3zh9rFxKkrmSdRp8Nojl65uTeEabk/CPgUdCYdDUiV2gPKEG19BChvoBHxN76AQS5N5lfZuIQouX7hMPHzIhBI5CEsuUZSnyf4kwlSHyDc2+MSDmJ0SPnHMpOiiIWyW6hKaeItmv8Yb90fWdbfGLINi/Ux2w26wt0llWiRcvqq0LTI695ajE8IF9dPjCSpIOmphCUoUQSIB1veFCVSBur/CHVvKvZKPnCStwjwiBuUoXOxQNyT6wo5LAJF4SWnFjkL+cI91cJ8cCEqc0BuLeZtC84GpUPhDHualbrVeGHKeoahRMHYdUlyp3vTSBq56w2uoNA7kxXqkHDqAT6wlUi6Bon5mFs3qk1U9VWbAslNyIYXVcwuAAIh+6rsSo2hBk7g6KUYXupACnXaiVaA6dREJc44tRGt4e9xXbuoIvHBTHFXKrnzhQ5oSkFRHHHCNVJSPWGdV7KPraLZFIKvs6+cOIoqjuBC8VoSZSqMs3O5VeOGUSDqTBGiieUPChpO6NehhPiAlyXQoZNJ5GPhKLJshowYCjtgC4SPUw8KYygd5SQfLW8AaoBKIkIJpj5TrlELRSXVeIq+UFfuiL2baWv4QoyDlu40kf2jAfFpeChb6J0sb2j76KSDcpJ9YJxS3VauKSk9EiFGjpOhK1XhPil3CQwZIJ2CR8I4WLDxQTJozad038zDiKQi+iU+hhDVBLwkKiTKrZUq+CYWKWs7o+cGCKeCLKNxyAEOppaL6gQgqiVxjCCxSVq2T/APrEiTw3NTqymTl1zCk7pbFyPhGoYXwMjEfaFt5LAZVqCDcxqdMw/I0lxD0rLNNzIbCFupHiifTQTTDNsFW1dfFAcoF3LF8P8DqnVZdL06+3INOC6QpN1D1EaZQOGdGo9LblZ+WZnXUKzF5SNSYLC8ASDa43iOHrqJNrc78otGU8UfmVSTVtRNubDyTzbMuyhKWGWkhIsnKgCwjqyNCDdQ2FojpdAC1A93rHyVpIC1Ky6a+USA4HQKIWndPqIKcwN7bxEUvcjwk66c4+zoKipJUi/OOoSlYUFABQ/dAE3RgWSFk5QCDa4uQOccWyGwBbOtXQaQ8CgpCEnKkfaMNryN91sFSvI6CE0CUEpkthxJuCm3LzhJZzEBV7enOHypCUlbpF7WA84S323Z5coF9vIQJAujuUy4CTZsWPM239IQEOXsTYaEAxKsU3OhX9k2hktJWM1yog3JP7oQhKHJsBRSBuQbpuN4QVrGmUgem8TmglAIeTnChoRyhATlcy3TZV9+UIWpQ7yUPKtY0GnIHSEoaWbgi3O3KHnFMkqKStZudo62EraythVzvmgAjubKOWFqFwohP2QBCiwVAFROo5DeJBCkEIsAbbfxhK0OgDslJseZhSuumG2z3gRZQHTaFBo5QCcwPO2sKS0VOBCz3tyRCULv2qmQUpGgUesDsuvcriW7k3sSNCLQgMoAN7Cx2I2hQZVfOVd4+I9I+TLqLnfsb735QJJ6I9Oq+KCEakAnnaI6pZFioKzH01iUiXVYpWAoJOg/jClSoQhSiBa24hdSkBA5q7pEkmWlWWk27WY77ht9mEVQrmppmTYVlC9VkckiIErWEtVJbS1gLEv3U3ivp1cD2I6ilxQs00m3leJfHjs1nXT2ULgSZnPPS6t6zNNyMqENABI0SIzCp1uZqVWl5CmG8zMq8dtG0jcmLrGFeBZdKVXUkZUiMtoGIVt45KUkFJlTmXroLRXVdUDIGjZWdHSkRlx3WjYgRL06USw2vOq1lrO6ldYyvEE52jzrT4HMJNuUX2JsQF9wJSrYW9BADV6hmUS4rXWx8ogVNQHGwVhTU5aLlDlUlFPrEu0Cpbiw2gAakkxO4g4SxU3S2mjR5l4MMpQ32YvpaKj+XMhh/EMtNO9m+5JqzBq+56wTzntJTc6O6zorbUxDaxhBzXupjnSgjKBZZp22KZTCzTFRoE62WLgdwxlmKHK+tK1po02lAvclBMb9UeN85PNKbVLXKuutogYMqU1jXHdEpTrITLzc0lb1xugHWJDGa6BA5zgCToib2X/ZQ+laXK4y4xS6++sPUyjbDTUOO/4R7A7JSZcHMUtpAShKRYBI2AHSGZ6dblVS8pL2AUQ02kckgQjElURTaXMOJI+qbsn12jQxCJjTbksxK+WV4Lue3kqV6aTNvPtyys3YaOEbDygUq86mU7TtO8gjmILJNhim4aYyBOeaHaur5lRjPMXvBDK1X1AJPlpEefutuVLp7OdZUk0ZatJcDDagtvp9mAStUlcqSUgqRe4NtQYNMDuNDCzj1w5MTj6iux1SBA/XKmn3lwaZOsQjbKCVYtuHEDksO4xVpVHwjOBGipo9kg22J3jL6bKNyMkwyq2VpAUvTmecGftJTK5qmSklT2yt597Mcv2bc4xyl12cYaTL1pp1JbNkuAE3A6wLIrsuOqJ0tnWKN8KoE3UZp/fMvKNNhGrUhpKGAoEXt3QRGE4LxTLy8/NsuqW2hToU2pSSM0aVLYsZDYyvN3I170I4ua6xCUWc24RhhaqtidnS8q7oXlN+kEc9UEpllqJudssYJQscSysR1JtuZau24NCuCyrYyQZKzLnbOWIQhvvFUEAdiElxa4RDM1NskkrAsdSdoG5qsompuzCswRpcbXgelqXWK8vNPKXLMqP6BG9vOC6j4RTLoSVJOlv/mHC2yEOuo7TjiiNTY/KJqC6gAqSSk7Kghaw22lCClIIVy84mJogQMlrgx2q6wQ+wha1AquR0tFjLtq8OtjtpFuiioaAJQSOqYlM0xNgUi/l1grrrKsl5RQ1uVdQRFgxKEkk/G4ixYpwHjQoHlblEhmVUk98aeY2hbpLJiVlFXuFA9dNbRN7BSbFOp5aaQ+1JhdilJSQdT1iclkEABBJPIQQKAhVipMuJvY256bRxNHQtJuBrztqYI26cpxKe0IAA0T1iUmlp7pQATzHSOXAoJfwHJVFOV2WbB/sbecDVb4NKUgGhqyH7XaDQRtDdMulJbFzca9YsEUlZALighXQQLoGPGoRNqHs2K8rV3htVaSuzqFPNAEqcaToIG3qGhlRzpO9u/oflHs80dhSLFKFDXxDT5QJ13h/Rpl1UwuSacd1uNh6xFkorasKkMrb6PC8rqkEpFsth6Q39GBfLKbchB7ifBM5Rp51xxn81Wu7RTrYdLRSCnqKdEHL1VpFbI8sNirFjQ8XCFHaWUA373naIZkAVG6fiRBc9Lob0zZj91AvEdNLmXiSzLdmk/acjmzLjEhf6LJ1SkeltIYcbS1oTr/AFRcwYnDyt31Lc6AaCG3KKU+BtKCPK8Lxx1XcEoLWy4vwN5f6xhlVPK9XMyoLnKQo6FJ167QwukLCrJsB5c4LjBCYUKrkQnSwt5CG1SItewvyFoJ1UlV9QLc4R9EDNZWv7hDrZhZNmFRnqW0hbl1WIV1hKZJsDxXi0eaDjrhyZrq5mFIklHUNptEvOOii5FWtSqLGySYdRKp+6bxZpkl9BDvuSiN7GOzE8kuUKsSx0QYcTKk7psIs0ydrXJ+cKTKJB71z8YXMV2UKAGAmw7vqYcTLpV9pR8gInJlhySCIfRLW2FoEkpQAq4SyQnRoq/tGGnGAQRlSL8hFuqXvtvEd2XBPdBX1hp+yNoVKqQaNzlHpCFyrKU6Axce5qXsj8Y4aatSe6j1vDBKcDVTltpIslOvOPg3p3U/KLf6KcNu7YHa0PN0NZIJJv5Q2bpwBUvYOAXA+UKTLPr1FxBI1RPXSJjVIFwCk+UcG3RIYl5CYKr594v5CVWhICrg89Yt2aRY3tY9TE5iRbvYrSAPnBCMkoSQFGk2VJA7unW8XkuyUoBLJV6RyXkWkm4dSfjFxJpaQNFpHXURbU8QA1UKV+uibl81+6xr5xaS/vAtkSlAhTTjCd1AjyiYibSSA02pZ9Ilho6qMXHopkqsqSA6deoMTkJbta6z8Ygs+8ufo2kJv96JbcrOOEZnUoH9UQ4CByTRFzupaShKdM1vPlC0zTTe7yQet4j/AEMXDd5bq7b3NrxLZprbSdEIT66mEJPREA1ffSSR4FLWfIQhdVmRo2lz1JtEtMlca3T02j4yLe61K+cMubdOhwCYbqk7zWlHmTeLBisKAAemVnqBEB1ltGlifOI5UBsj0iO+Bh5J5srkSMVmXQNc7hPMmJjVcQQcoQj4QH9qvnZPpHPeSjdVoYdSMTgldzRkamhe7h15XhCpxomwF1ctYDhVMp8RJ8ocbq5OwVpDJprbJ0SXCKS9n3UE+kNqUPvX+MULdTK9r3HOHkzxI8QHW5hBFZLmVuFJH2oSp1F9LH4xVe+oG7g+EKTUWxsVfAQoYlurMOX0AEcC1E8or0T6SbhKoeRNE62IgspSXU3WPt9Dv6xHS/fx3hQdAOyiDtA2XAp7QCElQP8A8wgruRZPzjtzsbDyEIiC4VR8CTyMdzW05R8VA845cu62uRb4xw5ftEfOOpyDXLf1MdBSfCkCOXJGVNtEk/GFpAt4QD6xzNrpyj7NbUQhSpK20rFlD5GOJQkDn84+K+pA+MNdtckJBWeYSIS9kVrp9KEgGwv8Y6Dbl+MNgPHwNEA81GHksOaZ3UIvySIbLl1l8E9dPMmFEItqoqPRIvHW2MviBc81HaJCLjSwSOVhAFyIAKMGAdmlkf1jaO+5hQ8OvS8SlKKQStVhDXb30bQtz4WhsvRAJtNNRqSn5wsU9NrBN7Q6gvqGoQ30J1MOiWC/0rri+oBsIHOVyi+6NN/pFNoPmY5+b3sglw/1E3ialllGiW0fHWHA4EDQBI8gITMVyr0tKJ+rl1noVaR8WX73CGm7fGJ5duNb69TpDZdGyyB8IS91ygqlXFnvPEeSUx1NN2zvOmJfbI5KJPpH2foj5mEJS3KbTIsp5FR8zeHEy6G9W0ISD84V2gH2gPIRwuJv4b2+MCUWqVYHTNrCcg+6SPSOpdO6UqA9LD8YYdnEJP1z6E+QN/3QJKSxT+XkQB6wopAiAqrsAWZDjp6JSYUmZmHU3RLBAOxdO0DmS5SpygLaC/lHA0q1wCB1iNabX4plDfk0P8YSZJtesw489f7yrfuhbrrWUlTrTX6V5tHUFV4tcMystW6mmVdXMdna5U23e8UzEsw34GkAdd4McBTwk6i8DoVI5WGURIpQ10zQ7ZR6pxbC4t3stOplMlqbLIZkmQkAeI+I+sOPr7JXe2HIGGZWeTOtBSFEJJsL84eKQUkE93mbxsmZcoy7LEOzZyX7qMQlawrP3Rv5xFUW33SpSlEJ8I2vEkLQLhN1W5gbQ04sNEKyDKdx/jDTk83dcWptKQLEpJ25CPnlNoCFXJTzF94Ul5JTok5FcxsIaXMNqJSU2SNCq41jrjqlF7p4qC1AsnMANLw2ttbqwrMARsnNtDalJy5ELuRzHIdI++rTqQsEcifFHXvuuAtspAS2SUuOZlW1A5Qy3MNouhvQD7R3iOh4Nl5QBDhFhY/gIWy4lUsQ6LKRr6wma6XLbdPJZTnzmxProIUXru2cdRk5JB5xF8bdyvukaAHUesJShLjai4SkJHdN9vWOzW2S5Qd1OKrKUXFoSkHU31iP7wz2lkqUrbQc4YmA19S0wc1hdayd4SttlIuXCom2g0sIRzylawWUj3ntXTnICQNLHePkqClEg5ki94jLfbUkKbbKgkeLaH23mikFCgE/d+6fOBDrnVFlsNAnW2jnLbWVLZ1VrqkQyFKdWVBQQ2g2TY6qPWEvPlYVbMm+xH2o72i3ENhgIBSNQeUCSCuyndIGeXUS6rMFbqvtCFzJBzCxRe1r7H/GG3Jns7jsiNbevnHzaAppRdzBCvD5ecBck2CctbUp9bjjLudB7xGtzv6Rzt0NDM73UrOwOxhrtGmG0pL2Yo2G8IW+2pvvDY3SOQhb25rg2/JSVl1s5kEKa3OvhEKL17JZsUqGhvEdDpdUoO3CXU6AHaOZfqkBvNlGxH2YS99l2Xqn1LzkIQrKU+Ik84X2gKi22o2A1JO0QVJShtTiVHKPCL6lXWFsukNguHIkC6z1MDmN9UpaLaJFQpKJpztyVsPITYKB3HnAYug4olMXKqMi9Jv0t5rs35darOXGxBg4Ycyudmt8rQq6iT+6Pm3c7Ti3e4kk97mYB0bXG+yJsj2gjdZNiVmtLmXGWabOPEqvmbF4y6eZx3S8UtTVIwhUJmSLeR9RJSUg849RMPBZzM520E2CidT5Q8h19K8qylIKe6oqPdhj4ZjjckqSKpzRYALy5UHsRPMtuyuHqq+46rLk7Mixvz8oHqzgTiVXJt2VlaQ3TmuzKu2cXcnyj14AG0KupTYJ7y+p6w26EpSS4pakXNk5tfWAbRsGpN06axx0AXjCkezJiQsibqbwW8tRKwVawaUb2dag42A+4GxpdJ8UelxL3VdaSobk8h5R3VASlgFTrp0PSD4IO6H4l9tFgDXs8pYl1LeezLzWsP4xeYT4SfyTxDIVhhRU5J37nkY2Uy6cxaUTci4vbfrCU9ksFpwFKxob/ahTCAk+JcRqqlzESf5QU5TxAz5gm6tzaKfiTicsyE2hagkm32tBqIm4jwBLYgdlHHpyZlnJF3tGlMG1z0gR4k4Fr1clXmKBNSylvtZQp/7CuRMA8zBpHVdG2EvaeiLp+vJTRpI5xb3ZFtdvOM1xZXxMtrCVXAQQLHc23hVMwzjxWHpeUrzEgudlG+z7eXVZDoG1gecCVXwFxFm0vimUaW7VSSlK3XhlAPO14WSV79LJyKKNmt0/gGsiTws72jiSpUwvXPe3lA5XawkuuKzWvvr4Y+w3wH4n4ZkmpaafplTl550urGaxlSeXnEyqezLizEL3Z1muy0tLDxNS1xnHQmGhE8907BPGWMEuvusMxTVU4irSzKntWpcZMwN8x8oVTMGmaJKmgpJ5EbR6NoPsuSlLQlKnwQgaADvQdUvghTqbNpUlZUwU95s2vfziQGOtYJkzM5ry1LcO5daAXJJpYH2cmvrD7HC2SeWAZNsJNu8BoPKPWx4YUpLgCSVrNu6LDToYWnA9JZdytN3yjQ6WTHZHN5pOK1y8qMcDqGSVGky6HValQRYqiXLcN5SlkplpRDIF73TrHq84Xp7rzTpZAWzttb1McnsN0eZmkKel0OLQTmCR4oM5uZTedo5LzLK4S7JObsyQNrbmLKUoS12yN5rdBqY9ByuDJJDiw00nItV7G1kDpC2cLU9qatJspUQLWtomEuUWcLBE0op7mUkch5+ULTS3FmwRoOfWNsVhWQZfccyJceULJQNkm+8MHDDMo0oqQFvKJsOnnCB10d1j6aW41qUkHkOkS5emn7SRmPMbCNDVhoh0jIVuqPLaEHDGVdkglA8REHmCRBTVPCkmw0G+u0Pe4JQiz4GU7G8GP8ly2oKUbaadB6wy/hhb6yopWpNtCNvWOzhJYoabk0IQMouBtrvExiVsgqQgE8iDtEtFAmG1rAJ7mxvofKFNUmZaBXqhN/tHQRwkC7KVxtlAbu7a/kdocZBsclh684kStJdm7lNsqTqo7CJbFGeU6UFJJ2Ch++DvdNnRRPeltNaISHD4dYS1NrI+vWL9QdvKJzmG3nXSGyXCDa4Oght7D65U/XXIOxJ0jsy4AKGZhLpu8tQSnYDnHFrS+D2aFuJHI8okOU11LgQhCnDa4XyEQZ1hTSiC/det0I3EJnShuqqqlT1OpWhtpkKI0UsBRjF8TYZVTKkXJ919xlarlChkSI1aquobzZS6m32VLtbzMZbjWuz8s13GJSYl3DYuZ7lPwivqwx7dVPpXOY7RNybcmUBDDbbfkB/GJwkGT4gNfjeA6m1FeYEbdILqdMPPgE5QPvExQSgjZXcZBC65SWQkm2h53iucpTdyQk6dOsEK1JG6tYjnIk3toPwhkOKeICG3KGHTdSbjpfQQwcPIzHKNDvblBK4BqtaglHIqOUCIJm+3UUyDSplY3URlQPXr8ILiO5LsoVCvDiQolCfx0iCumMhZDILyhuhsXHzgs+i1TZzVJ4ujk033UJ/jElEqywMqEJQANkx3HcOaThArOC1dawGwe90hYaUNkJEWU0hKZh8JAADht8oZIEaoqgso3ZWA1GvlCvdzfTX4QtSiLaxGU8srKSo2HKEuuspHu5tfT0j4sEjvWAhCEhQudTaFhIJNxyhV1ktDKU7rHwh5HZjqfhDSUJtsNoeaQDbQbQF0QAXR2d7lJ8o+yovdLRPXSHW0gKOghKnlpNgogQgF0hNkptCVDwAeZELUhCB3in4RFUTc6mFtoT2d7a9YQxBKHp7O3yF/hCkzA0CG7/CG20JypNhc7xJQLGwjhCCk4hSQ64SClA052hxImHNUm3kBDzIzC51I2iylEJKQSBqIdbC1KZCq5uXfcNlKWfSJjNJccI0i3YaQE3CRe+8TpdCV+IAxIZCE06QqoYoYNrqJ6gRcSNAbSQS2tZ8zaLRhtKD3UgbRLbWo6X2iXHG0clGfK5ck6UlJNmm0DncxaMyLaCMyx/dEIlEJVqoXMWTLaUoCgAD1h9rRyUdziusobSm+Q6czEpl1AGib36QmXQFJXcXh5GhCRoLbQeVNB108hfRu9uRhZuTfuIPSONpBG0fKACSQNRDTwnWG6SUjNqon4aCPiQn7I9YaKyVjU6xKaYQtBKkgm8MFOhQ3VBWlhfyERnWlKF7aDntDdZm3pbRhxSB5Q3T2UTSyZgZz5mI75LKSxlxdNultNwpxF+g1iOQFXyMur+FotzLNMNktNpSfIRxwANpIFireGc5ToaFSKZfJ7rSEDqo6x97s8fE6B/ZTFqUDXSGgAd4TMSjAsoaJK27jp+NoktyaPtBSvUw4RpDzYGWERLjbDaTogX6Wh1KRfa3oI6ALwsDeEJSgJaUbW0h4ADe8Mp8MOIN9+kCuToUkDb8IUHL7X+UNDXU7iFA3AJhLJLp3tNevqI4T1O8JULK0jiyRsYQhKu5SSbkx22UabdYgPzLqTZK1ARJk2kPfpU5vWAJRAJwzKE7rSD0jgfKj9Uha/QRMRLtI1ShIPpC9rWsIC5KXZQw2+s7JQPOFplFKPfUtXltEkiytITeB3S3SESiE7oHqTeHUthPhNvQWhsi5N4+KQNucJZLqn0hI319dY6FgbafCImdXU7wt1RQi6TYwK5Su0vsB8oT2g+2oJEUqJp1x0pW4op6RYoaSEXCReGnJwBSkzDSb27/naPlTqeRt67RDJ1tyiK4TmPrA2RWVqmZGbRQF+Rhz3gW1UPWKdPiEPJ0vaEsksrLt7/8AxvHe2uNClHrrFU44oEWUYUdEZhv1gXBKArPtQBqsEny0hLj1k+G4530gHrVanpYKDEy4gDoYVR1qqaUe/qU9ffMowIBKcyaXRU5UGGj9Y+0jyvcwhFTQ4v6hl90+SLJPxj5EjLyTYEqyhAt0vCA8skAqPw0jsqS4T3vE44fqZZlpHPO5rHyxNLNlTSGx/wCmgEwyVG+5hGY3Op02hMoXZlJEmhSj2zsw6eYK7Aw82zLM+Blu/Ui5iGVEpFyY6FkKsCbR2VdcqwLyk+EBI8haEqcP+MRCo9nuYSCe7ruDeAyrlOLlra2vtHQ4AeZv84gtqJdNzsNIeUo9mDc3jrJFOSsJHdGb+EPy82uVdQtCrKSbm43HQxVtKJUCSY7NLU20rISI4G2qEtvoVqGFsVLnGpmZn+wk5dnTOSADaL5qr/STSVS5ySy/CpX2/MR5xn3nJ1aWppa3G06hBUQLwS4DrU/UMVysrOzTzsuw0Q20pXdTFhS4m+4jcq6qwpljI3TyW5JeW2vskvtpPRI29Y6qZbWC0wkurVosnYwJvOrK20lSsqlkEX3ia0tTKXltKKVIb7pB2i6bUXOWypHU9hdXLqkMHs1uZz/s2+UNlaXQAkFK+Ry8or5AAUlt3+kcWcyuZitnZp1EsrK4oXVyMK6bKAbbrmRXNrojmAe3bVLZUlCbKuLWhLkx2S0pfdDqlaEAagdIppJ5x6lrcdWpS0g2UTtDitZVtZ8WS9+cKJL69V3CtoVZl9JcyhsNhOgO5hp2fZYdSlH1ylEXsNLxSvOKKwCo2SBbWFy+h08oTiEouENyrZ9WaayM5kMkXWrLexhKJsFSk9mtSQe8v70U7Uw527jedWTXS8PTi1IaSUEi19oQusbpQzSxVm7OoXMBhKMywMxtpCkzaEmy5ck7knYxUrHZySXEaLJ1Vz2hhucfccQlxxRBtcQuY3SZBayvEvF2YzuENgDupt++EkKczoQhAYuTn5mKhp1YdUjMcpOohx9akBeRRGU6WO0JmuNUuWysgkpWSp5fZck846p+y7Ad4ja1rxAacV2IczHOTqYYlVqczFZJJXYkwlwLAJct91aNqddCsyt9Ot/SEqbS2m8y9cDYX0EV5dW2VJQohJ3F4cYQHJV1SxmJ5mOBBXFtk/76hQBl2SWwbFdodUG3CEtk9kdTZPOG5dIUttBHdKNojVRamSENEpSeQhbaIQbmwU9pRcdzFstMoT9r7UILqkXUXw2wTom2phhp1b0sQ6oqtoLw0wkLmiFi4Sm4vygSbWCIBTS6nKlRSW2+hGpjqCJpYURlbT4UW1/+IjPfXFsO94abw6wShJWk2UDa/lC80h2vzUtBcU/kW02lKUko845dTjZ7UXSm5ULbxAkFqcnHM5Kr33MSJp5acyQohJXYiOBBCQtsbJ64cazoyrTyt9n1hMqDMNm7ZeU2NEHQ5v8ACIAbSy6ezGXP4rHfSITs080bNuLSE2tY7QLX6glFkvcBWb5WptIm0FC2QbIA21/dCgptlIV3X3XgRlH2REWXeW6++pxRUotakw7T1FFLK0myk5iD0jm2JXOJATy0raYUlRISs+ADUQpTndRolKiBcnS0VDMw67mccWpSwfETEF55b36RalXOusIXowxEHvEsg2cmGysnvEwp2faBSltsOq2CgIGpdhsnMUgkJ3h6VdW2tBQoi5N4EPKUsCunpvIRZICOeU63iNNONMkKdIdcXs0nnEOXJXKurUSVZjqY+kkjs3FEd641hdzZdawun20qWsF+yUDUpA0T5esfIfDpJSFZL2bsNT6wzUFFMwG0khGUHLCJVxTZeKCQRt8oQWBsl3F1JdeUk5VJKR9qw8UMKmCFFDiAEoTcC2/rFS/NPB1tfaLzKNib7woKK2FqWSVX3JjsyIN0VzJTDqpbtXkJbWo6LPJMMuTyTNloLBvqlVtL9TFUZp1bKUrcUU5rWvFiqVa91bORNyveFJJ0SAAHVOPu+7ZUNrDji7HPbaPgpDcqFEXST3tNSYgq+35KiS6kAIAGmUaQN9CUvRKeZecZaW24nsXCQop3HkYcUpKlFmWRZDQuVne0PraQ3JyyUJCQVk2HrEIHJP8Ad0zK184JzQCEIcXX8k6tCnRkaUW0k7gaq0hcstTzPZJQWGkWDqx4leUNPiwcUPEV2vDNWcVKtI93UW7pF7GBOhulBuLJwBMlMqLLRmMwtltt6mEJfQtxWdbbTgvmCtbDoIh1F9yWk2iwtSC4jvEHeOSsu2qVZWpAKlud4nnDfkE4NRcq0VL5kpzKLSFeEZdVfGGghlp4FSS7k+wgaE+cIqZJeykmyEd0X20imfnn0yiSl1QJ3MK5wBSMBcLq9SGXVKefbyj7LY2vHzerajM2ZS5skch5xUSLilSmdSiVKGpvC5glSmQokgjmY4PRFmtlKdZZaC3AgFCRdKbbnrEUIT7sXptouLWe43a2YQw84qznePdIt5Q8l1YJAUbBII8jCFwXWIUltppEuAUpZK9ezA/CH3Hm5VlDpsEjQItqBFA86tTzxUok5esMqeW66z2ilKs3zMIJVxjuiQTrEn4RdpRzeh84pqjUu3dK0qZsNUgqFvjFA66sBSQtVsw0vA9iZpOdRsbgjYmG31Dk7HALq2rVfeSsqDxaFjkSk6H4wHuYtZecdQxNplZhJP1bo/SHyMUT77j8pNJeWpaUrIAJ2EAOJ3V+65sxzNqISb6gWhgyknVScjQER4mxtLUzOubnEg3sUrFyT0vzEZDW68xW6x7xIIeZay95sKJST1tFBiadfn51pE46t5KbWCje0GeH6bKhDJDDdzqdN4ZnfkbrzUiFmY6J+kMPO5MjaiettIMZFlxhIL5y9U9YiMHsWR2XdueUIr8y7J0gvSy1Nu5rZgdbRVPJcVYtGVWk1UWJIAzLrbd9sx1PoIiGpTEybSUuW0n+mf7vxA5xBpss0JFuZyJL6zdSzqTpD6XFLbWVKJIGnlAZQLpwElPNsILmaeWuaXzC9ED0TEz35KQBlAQNgBYfARUvKIS1YnXeEKUStep02gS25RZrK3+kdb9PKGzOhSiVaD90VyFFLd0mxhkqJVqTC5Akzlf/2Q=="></div>
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
<div id="hint">Bienvenido a su hogar 🏡</div>
<div id="actionBanner" aria-live="polite"></div>
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
 const hint=document.getElementById('hint');if(hint)hint.textContent=p.pet_dead?'Descansa en paz 💖':p.pet_sick?'Está enferma: necesita medicina y cuidados 🤒':Number(p.pet_cleanliness)<42?'Necesita un baño y un buen cepillado 🫧':'Su hogar está listo: juega, dale comida y mimos 🏡';
 const sick=document.getElementById('sickAlert');if(sick)sick.style.display=p.pet_sick?'block':'none';
 const dead=document.getElementById('deadOverlay');if(dead)dead.style.display=p.pet_dead?'flex':'none';
 const poop=Number(p.pet_poop||0),medicine=Number(p.pet_medicine||0);
 for(const id of ['poopCount','poopCountTop']){const el=document.getElementById(id);if(el)el.textContent=poop;}
 for(const id of ['medicineCount','medicineCountTop']){const el=document.getElementById(id);if(el)el.textContent=medicine;}
 if(typeof syncPoopVisuals==='function')syncPoopVisuals(poop);
 if(typeof syncPetConditionVisuals==='function')syncPetConditionVisuals(p);
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
async function finishMiniGame(kind){const result=await window.act('mini_game',{game:kind});if(result){petReact('mini_game');document.getElementById('miniGameArea').innerHTML='<h3>🎉 ¡Lo has conseguido!</h3><p>'+result.message+'</p><button onclick="startMiniGame(&quot;'+kind+'&quot;)">Volver a jugar</button>';}}

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
// ---------------- HOGAR 3D DE LA MASCOTA ----------------
// Ocultamos el antiguo escenario de supervivencia y construimos un hogar cálido y legible.
for(const oldObj of [ground,grass,garden,sand,water,riverBed,river,riverBanks,cloudGroup,rain,moon,stars]){if(oldObj)oldObj.visible=false;}
for(const obj of resourceMeshes.values())obj.visible=false;
scene.background=new THREE.Color(0xb9d9ec);scene.fog=new THREE.Fog(0xb9d9ec,24,55);
sun.color.setHex(0xffedcf);sun.intensity=1.65;hemi.intensity=1.45;
const home=new THREE.Group();scene.add(home);
const mat=(color,roughness=0.82,metalness=0)=>new THREE.MeshStandardMaterial({color,roughness,metalness});
function homeBox(w,h,d,color,x,y,z,round=false){const g=round?new THREE.BoxGeometry(w,h,d):new THREE.BoxGeometry(w,h,d);const m=new THREE.Mesh(g,mat(color));m.position.set(x,y,z);m.castShadow=true;m.receiveShadow=true;home.add(m);return m;}
// Suelo de madera clara y alfombra central.
const homeFloor=new THREE.Mesh(new THREE.PlaneGeometry(20,20),mat(0xd8bd9b));homeFloor.rotation.x=-Math.PI/2;homeFloor.position.y=-.035;homeFloor.receiveShadow=true;home.add(homeFloor);
for(let z=-8;z<=8;z+=1.25){const seam=new THREE.Mesh(new THREE.BoxGeometry(19.8,.012,.018),mat(0xb99672));seam.position.set(0,.008,z);home.add(seam);}
const rug=new THREE.Mesh(new THREE.CylinderGeometry(4.3,4.3,.055,64),mat(0x9eaccf));rug.position.set(0,.015,-.1);rug.scale.z=.76;rug.receiveShadow=true;home.add(rug);
const rugInner=new THREE.Mesh(new THREE.CylinderGeometry(3.5,3.5,.018,64),mat(0xc7b9dc));rugInner.position.set(0,.05,-.1);rugInner.scale.z=.76;home.add(rugInner);
// Pared del fondo, zócalos y paredes laterales para que parezca una habitación real.
const backWall=homeBox(20,8,.35,0xf3dfd0,0,4,-8.6);backWall.receiveShadow=true;
homeBox(20,.28,.42,0xb58d75,0,.18,-8.34);
homeBox(.28,8,17,0xead6c8,-9.85,4,0);homeBox(.28,8,17,0xead6c8,9.85,4,0);
// Ventana grande con cielo y jardín al otro lado.
const windowGlass=homeBox(6.4,3.5,.10,0x9dd4e8,0,5.1,-8.37);windowGlass.material=mat(0x9ed7ec,.25,.05);windowGlass.material.emissive.setHex(0x31536a);windowGlass.material.emissiveIntensity=.18;
for(const x of [-3.35,3.35])homeBox(.18,3.85,.2,0xffffff,x,5.1,-8.25);
for(const y of [3.2,7])homeBox(6.85,.18,.2,0xffffff,0,y,-8.25);
homeBox(.13,3.5,.22,0xffffff,0,5.1,-8.22);homeBox(6.4,.13,.22,0xffffff,0,5.1,-8.22);
// Sol decorativo y nubes vistas a través de la ventana.
const windowSun=new THREE.Mesh(new THREE.CircleGeometry(.55,32),mat(0xffdf91));windowSun.position.set(2.15,6.0,-8.28);home.add(windowSun);
for(let i=0;i<4;i++){const cloudPuff=new THREE.Mesh(new THREE.SphereGeometry(.34+i%2*.1,12,10),mat(0xf7fbff));cloudPuff.position.set(-2.3+i*1.1,4.6+(i%2)*.3,-8.27);cloudPuff.scale.y=.55;home.add(cloudPuff);}
// Cama acolchada a la izquierda.
const bed=new THREE.Group();bed.position.set(-3.0,0,-1.85);home.add(bed);
const bedBase=new THREE.Mesh(new THREE.BoxGeometry(2.35,.32,1.65),mat(0x8c6f9f));bedBase.position.y=.22;bed.add(bedBase);
const bedMattress=new THREE.Mesh(new THREE.BoxGeometry(2.15,.25,1.45),mat(0xf2c5d8));bedMattress.position.set(0,.48,0);bed.add(bedMattress);
const bedPillow=new THREE.Mesh(new THREE.SphereGeometry(.43,18,12),mat(0xfff0df));bedPillow.position.set(0,.68,-.42);bedPillow.scale.set(1.25,.28,.7);bed.add(bedPillow);
for(const x of [-.95,.95]){const post=new THREE.Mesh(new THREE.SphereGeometry(.13,12,10),mat(0xc9a6dc));post.position.set(x,.58,.66);bed.add(post);}
// La comida, el agua y el baño se administran desde los botones; no hay objetos fijos en la habitación.
const foodBowl={rotation:{z:0}},waterBowl={rotation:{z:0}};
const bath={position:{x:0,z:0}},bathWater={position:{y:0},material:{opacity:.82}};
// Juguetes, estantería, plantas y luz ambiental.
// Sin pelota ni juguetes permanentes: el usuario inicia el juego desde los controles.
const toy={rotation:{z:0}},toyBall={position:{y:0}};
const shelf=homeBox(3.4,.18,.55,0xb68c6d,5.7,3.0,-7.95);homeBox(.16,1.5,.5,0x9d765b,4.1,3.7,-7.95);homeBox(.16,1.5,.5,0x9d765b,7.3,3.7,-7.95);
for(let i=0;i<3;i++){const pot=new THREE.Mesh(new THREE.CylinderGeometry(.22,.28,.38,12),mat(0xd68f83));pot.position.set(-7.4+i*.8,.19,-6.9);home.add(pot);const leaf=new THREE.Mesh(new THREE.SphereGeometry(.45,12,10),mat(0x5d9e75));leaf.position.set(-7.4+i*.8,.72,-6.9);leaf.scale.set(.65,1.2,.65);home.add(leaf);}
const warmLight=new THREE.PointLight(0xffd6a1,34,14);warmLight.position.set(-4.8,5.8,1);home.add(warmLight);
// No se dibujan bañera, ducha, agua, comida ni juguetes en el escenario.
const foamBubbles=[],showerDrops=[];
const dirtySpots=new THREE.Group();playerMesh.add(dirtySpots);
for(let i=0;i<9;i++){const spot=new THREE.Mesh(new THREE.SphereGeometry(.075+(i%3)*.025,10,8),mat(0x8a674a));spot.position.set((i%3-1)*.35,.78+Math.floor(i/3)*.22,.64);spot.scale.set(1,.65,.35);dirtySpots.add(spot);}
const sickMark=new THREE.Group();playerMesh.add(sickMark);
const crossMat=new THREE.MeshStandardMaterial({color:0xff526a,emissive:0x7b1024,emissiveIntensity:.45});
const crossA=new THREE.Mesh(new THREE.BoxGeometry(.12,.58,.10),crossMat),crossB=new THREE.Mesh(new THREE.BoxGeometry(.58,.12,.10),crossMat);crossA.position.y=3.1;crossB.position.y=3.1;sickMark.add(crossA,crossB);
function syncPetConditionVisuals(p){
 const dirty=Number(p.pet_cleanliness??100)<42;dirtySpots.visible=dirty;sickMark.visible=!!p.pet_sick;
 if(p.pet_dead){dirtySpots.visible=false;sickMark.visible=false;}
}
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
function petReact(action){
 const now=performance.now();
 const durations={feed_kibble:4200,feed_fruit:4200,feed_fish:4500,give_water:3600,clean_pet:5200,play_pet:3600,mini_game:4200,sleep_pet:5000,give_medicine:3800,adopt_pet:3000};
 const duration=durations[action]||3000;
 playerMesh.userData.reactUntil=now+duration;
 playerMesh.userData.react=action;playerMesh.userData.reactStarted=now;playerMesh.userData.reactDuration=duration*.001;
 playerMesh.userData.actionOrigin=new THREE.Vector3(playerMesh.position.x,0,playerMesh.position.z);
 const destinations={sleep_pet:[-3,-1.85]};
 const dest=destinations[action]||[playerMesh.position.x,playerMesh.position.z];playerMesh.userData.actionTarget=new THREE.Vector3(dest[0],0,dest[1]);
 const labels={feed_kibble:'🥣 ¡Ñam, ñam! Comiendo pienso',feed_fruit:'🍎 ¡Qué rica fruta!',feed_fish:'🐟 ¡Su comida favorita!',give_water:'💧 Glup, glup… bebiendo agua',clean_pet:'🫧 ¡Hora del baño! Chapoteando',play_pet:'🎾 ¡A jugar!',mini_game:'⭐ ¡Lo está pasando genial!',sleep_pet:'🌙 A descansar calentito',give_medicine:'💊 Tomando su medicina',adopt_pet:'💖 ¡Un nuevo comienzo!'};
 const banner=document.getElementById('actionBanner');if(banner){banner.textContent=labels[action]||'🐾 ¡Qué bien!';banner.classList.add('show');clearTimeout(window.petBannerTimer);window.petBannerTimer=setTimeout(()=>banner.classList.remove('show'),durations[action]||3000);}
}
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
 sun.intensity=.75+daylight*1.25;
 sun.color.set(dayFactor>.22?0xffe4b5:0xbac9ed);
 hemi.intensity=.9+daylight*.8;
 scene.background.set(dayFactor>.2?0xb9d9ec:0x778aa8);
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
 // La mascota camina hasta el objeto de la acción y vuelve a su alfombra.
 if(reaction&&playerMesh.userData.actionOrigin&&playerMesh.userData.actionTarget){const total=playerMesh.userData.reactDuration||3;const approach=.82,returnAt=Math.max(approach,total-.78);let f=reactTime<approach?THREE.MathUtils.smoothstep(reactTime,0,approach):reactTime>returnAt?1-THREE.MathUtils.smoothstep(reactTime,returnAt,total):1;playerMesh.position.x=THREE.MathUtils.lerp(playerMesh.userData.actionOrigin.x,playerMesh.userData.actionTarget.x,f);playerMesh.position.z=THREE.MathUtils.lerp(playerMesh.userData.actionOrigin.z,playerMesh.userData.actionTarget.z,f);const dx=playerMesh.userData.actionTarget.x-playerMesh.userData.actionOrigin.x,dz=playerMesh.userData.actionTarget.z-playerMesh.userData.actionOrigin.z;if(Math.abs(dx)+Math.abs(dz)>.1)playerMesh.rotation.y=Math.atan2(dx,dz);}else if(!reaction&&playerMesh.userData.actionOrigin){playerMesh.userData.actionOrigin=null;playerMesh.userData.actionTarget=null;}
 // Las acciones se muestran con animaciones de la mascota, sin objetos fijos en la habitación.
 if(reaction==='play_pet'||reaction==='mini_game'){playerMesh.scale.setScalar(petGrowthScale*(1+Math.max(0,Math.sin(t*15))*.16));arms[0].rotation.z=Math.sin(t*15)*.72;arms[1].rotation.z=-Math.sin(t*15)*.72;legs[0].rotation.x=Math.sin(t*15)*.65;legs[1].rotation.x=-Math.sin(t*15)*.65;}
 else if(reaction==='feed_kibble'||reaction==='feed_fruit'||reaction==='feed_fish'){const chew=Math.sin(reactTime*13);headPivot.rotation.x=-.28+Math.max(0,chew)*.34;headPivot.rotation.z=Math.sin(t*10)*.035;playerMesh.scale.setScalar(petGrowthScale*(1+Math.max(0,chew)*.045));arms[0].rotation.x=-.7+Math.sin(t*13)*.08;arms[1].rotation.x=-.7+Math.sin(t*13)*.08;}
 else if(reaction==='give_water'){headPivot.rotation.x=-.42+Math.sin(reactTime*8)*.07;arms[0].rotation.x=-.45;arms[1].rotation.x=-.45;playerMesh.scale.setScalar(petGrowthScale*(1+Math.sin(reactTime*9)*.025));}
 else if(reaction==='clean_pet'){playerMesh.scale.setScalar(petGrowthScale*(1+Math.sin(reactTime*18)*.035));headPivot.rotation.z=Math.sin(reactTime*10)*.12;arms[0].rotation.z=Math.sin(t*12)*.48;arms[1].rotation.z=-Math.sin(t*12)*.48;}
 else if(reaction==='sleep_pet'){playerMesh.scale.set(petGrowthScale*.98,petGrowthScale*.78,petGrowthScale);playerMesh.position.y=.02;headPivot.rotation.x=.38;ears[0].rotation.z=-.65;ears[1].rotation.z=.65;}
 else if(reaction==='give_medicine'){headPivot.rotation.x=.22+Math.sin(t*7)*.04;arms[0].rotation.z=-.28;arms[1].rotation.z=.28;}
 else if(player?.userData?.pet_sick||player?.pet_sick){headPivot.rotation.x=.25;headPivot.rotation.z=.07;ears[0].rotation.z=-.62;ears[1].rotation.z=.62;playerMesh.scale.lerp(new THREE.Vector3(petGrowthScale*.98,petGrowthScale*.96,petGrowthScale),.08);}
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
    start_pet_notification_monitor()
    if request.path != "/telegram/webhook":
        set_telegram_webhook()


if __name__ == "__main__":
    if not BOT_TOKEN:
        log.warning("Configura BOT_TOKEN antes de iniciar el bot")
    if not PUBLIC_URL:
        log.warning("Configura PUBLIC_URL con la URL pública de Render")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
