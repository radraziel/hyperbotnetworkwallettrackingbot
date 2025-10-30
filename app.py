# app.py
# ------------------------------------------------------------
# Hyperbot Network Wallet Tracking Bot
# - Telegram bot con /start, /walletsus, /status, /stop
# - Monitoreo de páginas públicas de hyperbot.network
# - Servidor HTTP (FastAPI) para health checks en Render Web Service
# ------------------------------------------------------------

import os
import re
import time
import json
import html
import asyncio
import logging
import datetime as dt
import threading
from typing import Optional, Dict, Any, List, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn

# --- Telegram (python-telegram-bot v21) ---
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# Configuración general
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hyperbot-telegram")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en variables de entorno.")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; WalletWatchBot/1.0)")
HYPERBOT_BASE = "https://hyperbot.network"

REDIS_URL = os.getenv("REDIS_URL", "")
USE_REDIS = bool(REDIS_URL)
if USE_REDIS:
    import redis

# =========================
# Persistencia (Redis opcional)
# =========================
class Storage:
    def __init__(self):
        self.memory: Dict[str, str] = {}
        self.last_event: Dict[str, str] = {}
        self.last_check: Dict[str, float] = {}
        if USE_REDIS:
            self.r = redis.from_url(REDIS_URL, decode_responses=True)
        else:
            self.r = None

    def _k(self, chat_id: int, suffix: str) -> str:
        return f"chat:{chat_id}:{suffix}"

    def set_wallet(self, chat_id: int, addr: str) -> None:
        key = self._k(chat_id, "wallet")
        if self.r:
            self.r.set(key, addr)
        else:
            self.memory[key] = addr

    def get_wallet(self, chat_id: int) -> Optional[str]:
        key = self._k(chat_id, "wallet")
        if self.r:
            return self.r.get(key)
        else:
            return self.memory.get(key)

    def set_last_event_id(self, chat_id: int, wallet: str, event_id: str) -> None:
        key = self._k(chat_id, f"last_event:{wallet.lower()}")
        if self.r:
            self.r.set(key, event_id)
        else:
            self.last_event[key] = event_id

    def get_last_event_id(self, chat_id: int, wallet: str) -> Optional[str]:
        key = self._k(chat_id, f"last_event:{wallet.lower()}")
        if self.r:
            return self.r.get(key)
        else:
            return self.last_event.get(key)

    def set_last_check(self, chat_id: int) -> None:
        key = self._k(chat_id, "last_check_ts")
        ts = time.time()
        if self.r:
            self.r.set(key, str(ts))
        else:
            self.last_check[key] = ts

    def get_last_check(self, chat_id: int) -> Optional[float]:
        key = self._k(chat_id, "last_check_ts")
        if self.r:
            val = self.r.get(key)
            return float(val) if val else None
        else:
            return self.last_check.get(key)

STORE = Storage()

# =========================
# Cliente HTTP
# =========================
def client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers=headers, follow_redirects=True)

# =========================
# Extracción de eventos desde hyperbot.network
# =========================
class HyperbotExtractor:
    TRADER_PATH = "/trader/{addr}"

    @staticmethod
    def _trader_url(addr: str) -> str:
        return f"{HYPERBOT_BASE}{HyperbotExtractor.TRADER_PATH.format(addr=addr)}"

    @staticmethod
    def _normalize_addr(addr: str) -> str:
        return addr.strip().lower()

    @staticmethod
    def _parse_next_data(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return None
        try:
            data = json.loads(tag.text)
            return data
        except Exception:
            return None

    @staticmethod
    def _heuristic_event_lines(soup: BeautifulSoup) -> List[str]:
        keywords = ["perp", "trade", "order", "opened", "closed", "created", "filled", "recent", "position"]
        texts: List[str] = []
        for el in soup.find_all(text=True):
            t = " ".join(el.strip().split())
            if len(t) < 4:
                continue
            if any(k in t.lower() for k in keywords):
                texts.append(t)
        seen = set()
        uniq = []
        for t in texts:
            if t not in seen:
                uniq.append(t)
                seen.add(t)
        return uniq[:500]

    @staticmethod
    def _build_event_id(s: str) -> str:
        return str(abs(hash(s)) % (10**16))

    @staticmethod
    def _format_event_text(raw: str) -> str:
        raw = html.unescape(raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        raw = re.sub(r"(Valor de posición\s*=\s*\$?[0-9\.,]+)", r"💰 *\1*", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(Buy|Long|Opened|Open)\b", r"🟢 \1", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(Sell|Short|Closed|Close)\b", r"🔴 \1", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(Order|Trade|Filled|Fill)\b", r"📄 \1", raw, flags=re.IGNORECASE)
        return raw

    @classmethod
    async def fetch_events(cls, addr: str) -> List[Tuple[str, str]]:
        addr = cls._normalize_addr(addr)
        url = cls._trader_url(addr)
        async with client() as c:
            resp = await c.get(url)
            resp.raise_for_status()
            html_text = resp.text

        soup = BeautifulSoup(html_text, "html.parser")
        events: List[Tuple[str, str]] = []
        next_data = cls._parse_next_data(soup)
        if next_data:
            def walk(node, acc):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k.lower() in ("events", "activities", "activity", "trades", "orders", "recent"):
                            try:
                                for item in v:
                                    text = json.dumps(item, ensure_ascii=False)
                                    eid = cls._build_event_id(text)
                                    acc.append((eid, text))
                            except Exception:
                                pass
                        walk(v, acc)
                elif isinstance(node, list):
                    for it in node:
                        walk(it, acc)
            tmp: List[Tuple[str, str]] = []
            walk(next_data, tmp)
            for eid, raw in tmp:
                events.append((eid, cls._format_event_text(raw)))

        if not events:
            lines = cls._heuristic_event_lines(soup)
            for raw in lines:
                eid = cls._build_event_id(raw)
                events.append((eid, cls._format_event_text(raw)))

        seen = set()
        dedup = []
        for eid, txt in events:
            if eid not in seen:
                dedup.append((eid, txt))
                seen.add(eid)
        return dedup[:100]

# =========================
# Formato de mensajes
# =========================
def format_alert(addr: str, event_text: str) -> str:
    link = f"{HYPERBOT_BASE}/trader/{addr.lower()}"
    ts = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"🔔 *Actividad detectada*\n"
        f"👛 Wallet: `{addr}`\n"
        f"{event_text}\n"
        f"⏱ {ts}\n"
        f"[Ver en Hyperbot]({link})"
    )

def format_help() -> str:
    return (
        "Hola 👋\n\n"
        "*Comandos disponibles:*\n"
        "• `/walletsus <direccion>` — Suscribirte a una wallet (reemplaza la anterior).\n"
        "• `/status` — Ver la wallet suscrita y último chequeo.\n"
        "• `/stop` — Dejar de monitorear.\n\n"
        "_Ejemplo:_\n"
        "`/walletsus 0xc2a30212a8ddac9e123944d6e29faddce994e5f2`\n\n"
        "Recibirás alertas cuando detecte actividad en Hyperbot."
    )

# =========================
# Handlers de comandos
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir Hyperbot", url="https://hyperbot.network")]])
    await update.message.reply_text(format_help(), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    last = STORE.get_last_check(chat_id)
    if wal:
        t = dt.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S UTC") if last else "—"
        await update.message.reply_text(f"👛 Wallet actual: `{wal}`\nÚltimo chequeo: {t}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No tienes una wallet suscrita. Usa `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    if wal:
        STORE.set_wallet(chat_id, "")
        await update.message.reply_text("✅ Monitoreo detenido. Puedes suscribirte de nuevo con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No hay monitoreo activo.", parse_mode=ParseMode.MARKDOWN)

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: `/walletsus <direccion>`", parse_mode=ParseMode.MARKDOWN)
        return
    addr = context.args[0].strip()
    if not ADDR_RE.match(addr):
        await update.message.reply_text("Dirección inválida. Debe comenzar con `0x` y tener 42 caracteres.", parse_mode=ParseMode.MARKDOWN)
        return
    STORE.set_wallet(chat_id, addr)
    STORE.set_last_event_id(chat_id, addr, "")
    await update.message.reply_text(f"✅ Monitoreando la wallet:\n`{addr}`\n\nTe enviaré alertas cuando detecte actividad en Hyperbot.", parse_mode=ParseMode.MARKDOWN)

# =========================
# Loop de monitoreo
# =========================
async def monitor_loop(bot_app):
    await asyncio.sleep(2)
    extractor = HyperbotExtractor()
    while True:
        try:
            chats_wallets: List[Tuple[int, str]] = []
            if USE_REDIS:
                cursor = 0
                while True:
                    cursor, keys = STORE.r.scan(cursor=cursor, match="chat:*:wallet", count=100)
                    for k in keys:
                        addr = STORE.r.get(k) or ""
                        if addr:
                            chat_id = int(k.split(":")[1])
                            chats_wallets.append((chat_id, addr))
                    if cursor == 0:
                        break
            else:
                for k, v in STORE.memory.items():
                    if k.endswith(":wallet") and v:
                        chat_id = int(k.split(":")[1])
                        chats_wallets.append((chat_id, v))

            if not chats_wallets:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            for chat_id, addr in chats_wallets:
                try:
                    events = await extractor.fetch_events(addr)
                    last_id = STORE.get_last_event_id(chat_id, addr) or ""
                    if not last_id and events:
                        STORE.set_last_event_id(chat_id, addr, events[0][0])
                        STORE.set_last_check(chat_id)
                        continue

                    new_events = []
                    for eid, txt in events:
                        if eid == last_id:
                            break
                        new_events.append((eid, txt))
                    if new_events:
                        for eid, txt in reversed(new_events):
                            msg = format_alert(addr, txt)
                            await bot_app.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
                        STORE.set_last_event_id(chat_id, addr, new_events[0][0])
                    STORE.set_last_check(chat_id)
                except Exception as e:
                    logger.error(f"Error monitoreando {addr}: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.exception(f"Loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# =========================
# Servidor HTTP para Render
# =========================
app_http = FastAPI()

@app_http.get("/")
def root():
    return {"ok": True, "service": "hyperbot-network-wallet-tracking-bot"}

@app_http.get("/healthz")
def health():
    return {"status": "healthy"}

def run_http_server():
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app_http, host="0.0.0.0", port=port, log_level="warning")

# =========================
# Main
# =========================
def main():
    threading.Thread(target=run_http_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("walletsus", subs_cmd))
    app.add_handler(CommandHandler("status", stat_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    threading.Thread(target=lambda: asyncio.run(monitor_loop(app)), daemon=True).start()

    logger.info("Bot iniciado (polling + HTTP healthcheck).")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
