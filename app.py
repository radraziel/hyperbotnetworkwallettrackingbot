# app.py
# ------------------------------------------------------------
# Hyperbot Network Wallet Tracking Bot (Py 3.13 stable, async)
# - /start, /walletsus, /status, /checknow, /stop
# - Extracción en cascada (NEXT_DATA -> URLs JSON/CSV descubiertas -> heurística)
# - Healthcheck FastAPI para Render
# - Arranque asíncrono: initialize/start/polling/idle con asyncio.run(...)
# ------------------------------------------------------------

import os
import re
import time
import csv
import io
import json
import html
import hashlib
import asyncio
import logging
import datetime as dt
import threading
from typing import Optional, Dict, Any, List, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
import uvicorn

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Application,
    CallbackContext,
)

# =========================
# Config
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hyperbot-telegram")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en variables de entorno.")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; WalletWatchBot/1.0)")
HYPERBOT_BASE = "https://hyperbot.network"
PAGE_CHANGE_ALERTS = os.getenv("PAGE_CHANGE_ALERTS", "false").lower() in ("1", "true", "yes")

REDIS_URL = os.getenv("REDIS_URL", "")
USE_REDIS = bool(REDIS_URL)
if USE_REDIS:
    import redis  # opcional

# =========================
# Storage
# =========================
class Storage:
    def __init__(self):
        self.memory: Dict[str, str] = {}
        self.last_event: Dict[str, str] = {}
        self.last_check: Dict[str, float] = {}
        self.page_hash: Dict[str, str] = {}
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
        return self.r.get(key) if self.r else self.memory.get(key)

    def set_last_event_id(self, chat_id: int, wallet: str, event_id: str) -> None:
        key = self._k(chat_id, f"last_event:{wallet.lower()}")
        if self.r:
            self.r.set(key, event_id)
        else:
            self.last_event[key] = event_id

    def get_last_event_id(self, chat_id: int, wallet: str) -> Optional[str]:
        key = self._k(chat_id, f"last_event:{wallet.lower()}")
        return self.r.get(key) if self.r else self.last_event.get(key)

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
        return self.last_check.get(key)

    def set_page_hash(self, chat_id: int, wallet: str, h: str) -> None:
        key = self._k(chat_id, f"page_hash:{wallet.lower()}")
        if self.r:
            self.r.set(key, h)
        else:
            self.page_hash[key] = h

    def get_page_hash(self, chat_id: int, wallet: str) -> Optional[str]:
        key = self._k(chat_id, f"page_hash:{wallet.lower()}")
        return self.r.get(key) if self.r else self.page_hash.get(key)

STORE = Storage()

# =========================
# HTTP client
# =========================
def client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0), headers=headers, follow_redirects=True)

# =========================
# Utils
# =========================
def _bold_value_position(txt: str) -> str:
    return re.sub(r"(Valor de posición\s*=\s*\$?[0-9\.,]+)", r"💰 *\1*", txt, flags=re.IGNORECASE)

def _format_line(raw: str) -> str:
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = _bold_value_position(raw)
    raw = re.sub(r"\b(Buy|Long|Opened|Open)\b", r"🟢 \1", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(Sell|Short|Closed|Close)\b", r"🔴 \1", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(Order|Trade|Filled|Fill)\b", r"📄 \1", raw, flags=re.IGNORECASE)
    return raw

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]

# =========================
# Extractor
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
    def _parse_next_data(soup: BeautifulSoup) -> List[str]:
        out: List[str] = []
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return out
        try:
            data = json.loads(tag.text)
        except Exception:
            return out

        def walk(node, acc: List[str]):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k.lower() in ("events", "activities", "activity", "trades", "orders", "recent", "positions", "fills"):
                        try:
                            for item in v:
                                acc.append(json.dumps(item, ensure_ascii=False))
                        except Exception:
                            pass
                    walk(v, acc)
            elif isinstance(node, list):
                for it in node:
                    walk(it, acc)

        tmp: List[str] = []
        walk(data, tmp)
        return tmp

    @staticmethod
    def _discover_api_urls(addr: str, html_text: str) -> List[str]:
        addr_l = addr.lower()
        candidates: List[str] = []
        url_re = re.compile(r"""(?P<u>https?://[^\s"'<>]+|/[A-Za-z0-9_\-\/\.\?\=&%]+)""")
        for m in url_re.finditer(html_text):
            u = m.group("u")
            if addr_l in u.lower():
                if any(ext in u.lower() for ext in (".json", ".csv")) or ("/api/" in u.lower()):
                    candidates.append(u)
        seen = set()
        uniq = []
        for u in candidates:
            if u not in seen:
                uniq.append(u)
                seen.add(u)
        return uniq[:10]

    @staticmethod
    def _abs_url(u: str) -> str:
        if u.startswith("http://") or u.startswith("https://"):
            return u
        return HYPERBOT_BASE.rstrip("/") + "/" + u.lstrip("/")

    @staticmethod
    def _parse_csv_text(text: str) -> List[str]:
        out: List[str] = []
        try:
            f = io.StringIO(text)
            reader = csv.DictReader(f)
            for row in reader:
                sym = row.get("Symbol") or row.get("symbol") or row.get("pair") or ""
                act = row.get("Action") or row.get("action") or ""
                size = row.get("Size") or row.get("size") or ""
                price = row.get("Price") or row.get("price") or ""
                pnl = row.get("Closed PnL") or row.get("pnl") or row.get("ROE") or ""
                line = f"{sym} {act} size={size} price={price} pnl={pnl}".strip()
                out.append(_format_line(line))
        except Exception:
            return []
        return out

    @staticmethod
    def _parse_json(obj: Any) -> List[str]:
        out: List[str] = []
        def walk(node):
            if isinstance(node, dict):
                keys = set(k.lower() for k in node.keys())
                if {"symbol", "action", "size"} <= keys or {"pair", "side", "size"} <= keys:
                    sym = node.get("symbol") or node.get("pair") or ""
                    act = node.get("action") or node.get("side") or ""
                    size = node.get("size") or node.get("qty") or node.get("amount") or ""
                    price = node.get("price") or node.get("entry") or ""
                    pnl = node.get("pnl") or node.get("roe") or ""
                    line = f"{sym} {act} size={size} price={price} pnl={pnl}"
                    out.append(_format_line(line))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for it in node:
                    walk(it)
        walk(obj)
        return out

    @classmethod
    async def fetch_events(cls, addr: str) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
        addr = cls._normalize_addr(addr)
        url = cls._trader_url(addr)
        meta: Dict[str, Any] = {"reason": "", "page_hash": "", "discovered_urls": []}

        async with client() as c:
            resp = await c.get(url)
            resp.raise_for_status()
            html_text = resp.text

        page_hash = _hash(html_text)
        meta["page_hash"] = page_hash

        soup = BeautifulSoup(html_text, "html.parser")
        events_texts: List[str] = []

        # 1) __NEXT_DATA__
        next_lines = cls._parse_next_data(soup)
        if next_lines:
            events_texts.extend(next_lines)

        # 2) Descubrir endpoints y consultarlos
        if not events_texts:
            discovered = cls._discover_api_urls(addr, html_text)
            meta["discovered_urls"] = discovered
            async with client() as c:
                for u in discovered:
                    absu = cls._abs_url(u)
                    try:
                        r = await c.get(absu)
                        if r.status_code != 200:
                            continue
                        ctype = r.headers.get("content-type", "")
                        if "application/json" in ctype or r.text.strip().startswith("{") or r.text.strip().startswith("["):
                            try:
                                obj = r.json()
                                events_texts.extend(cls._parse_json(obj))
                            except Exception:
                                pass
                        elif "text/csv" in ctype or absu.lower().endswith(".csv"):
                            events_texts.extend(cls._parse_csv_text(r.text))
                    except Exception as e:
                        logger.warning(f"Fallo al leer {absu}: {e}")

        # 3) Fallback heurístico
        if not events_texts:
            keywords = ["perp", "trade", "order", "opened", "closed", "created", "filled", "recent", "position", "entry", "liq", "value"]
            texts: List[str] = []
            for el in soup.find_all(string=True):
                t = " ".join(el.strip().split())
                if len(t) < 4:
                    continue
                if any(k in t.lower() for k in keywords):
                    texts.append(t)
            buf = ""
            combined: List[str] = []
            for t in texts:
                if buf:
                    buf += " | " + t
                else:
                    buf = t
                if t.endswith(".") or "•" in t or len(buf) > 200:
                    combined.append(buf)
                    buf = ""
            if buf:
                combined.append(buf)
            events_texts.extend(_format_line(x) for x in combined)

        events: List[Tuple[str, str]] = []
        seen = set()
        for raw in events_texts:
            eid = str(abs(hash(raw)) % (10**16))
            if eid in seen:
                continue
            seen.add(eid)
            events.append((eid, raw))

        meta["reason"] = "ok" if events else "no_events_found"
        return events[:100], meta

# =========================
# Mensajes
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
        "*Comandos:*\n"
        "• `/walletsus <direccion>` — Suscribirte (reemplaza la anterior)\n"
        "• `/status` — Wallet suscrita y último chequeo\n"
        "• `/checknow` — Forzar snapshot inmediato\n"
        "• `/stop` — Detener monitoreo\n\n"
        "_Ejemplo:_ `/walletsus 0xc2a30212a8ddac9e123944d6e29faddce994e5f2`"
    )

def _utc_now() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# =========================
# Handlers
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir Hyperbot", url="https://hyperbot.network")]])
    await update.message.reply_text(format_help(), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    last = STORE.get_last_check(chat_id)
    t = dt.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S UTC") if last else "—"
    if wal:
        await update.message.reply_text(f"👛 Wallet actual: `{wal}`\nÚltimo chequeo: {t}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No tienes una wallet suscrita. Usa `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    STORE.set_wallet(chat_id, "")
    await update.message.reply_text("✅ Monitoreo detenido. Puedes suscribirte de nuevo con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

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
    await update.message.reply_text(
        f"✅ Monitoreando la wallet:\n`{addr}`\n\nTe enviaré alertas cuando detecte actividad en Hyperbot.",
        parse_mode=ParseMode.MARKDOWN
    )

async def checknow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    if not wal:
        await update.message.reply_text("Primero suscríbete con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text("⏳ Checando ahora mismo en Hyperbot...", parse_mode=ParseMode.MARKDOWN)
    events, meta = await HyperbotExtractor.fetch_events(wal)
    if events:
        sample = "\n".join(f"• {txt}" for _, txt in events[:3])
        await update.message.reply_text(
            f"✅ Encontré {len(events)} posibles eventos (muestra):\n{sample}\n\nHash página: `{meta.get('page_hash','')}`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        msg = (
            "⚠️ *No encontré eventos estructurados en esta revisión.*\n"
            f"Motivo: `{meta.get('reason','')}`\n"
            f"Hash página: `{meta.get('page_hash','')}`\n"
        )
        if meta.get("discovered_urls"):
            urls = "\n".join("• " + u for u in meta["discovered_urls"])
            msg += f"Posibles endpoints descubiertos:\n{urls}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# =========================
# Monitor loop
# =========================
async def monitor_loop(bot_app: Application):
    await asyncio.sleep(2)
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
                for k, v in list(STORE.memory.items()):
                    if k.endswith(":wallet") and v:
                        chat_id = int(k.split(":")[1])
                        chats_wallets.append((chat_id, v))

            if not chats_wallets:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            for chat_id, addr in chats_wallets:
                try:
                    events, meta = await HyperbotExtractor.fetch_events(addr)
                    last_id = STORE.get_last_event_id(chat_id, addr) or ""
                    page_hash_prev = STORE.get_page_hash(chat_id, addr)
                    STORE.set_page_hash(chat_id, addr, meta.get("page_hash", ""))

                    if not events and PAGE_CHANGE_ALERTS and page_hash_prev and page_hash_prev != meta.get("page_hash"):
                        try:
                            await bot_app.bot.send_message(
                                chat_id=chat_id,
                                text=(f"ℹ️ *La página de Hyperbot cambió para*\n`{addr}`\n"
                                      f"_Puede haber actividad nueva, pero no fue legible por el parser._\n"
                                      f"{_utc_now()}"),
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception as e:
                            logger.warning(f"Send page-change failed chat {chat_id}: {e}")

                    if events:
                        new_events: List[Tuple[str, str]] = []
                        if not last_id:
                            STORE.set_last_event_id(chat_id, addr, events[0][0])
                        else:
                            for eid, txt in events:
                                if eid == last_id:
                                    break
                                new_events.append((eid, txt))
                            if new_events:
                                for eid, txt in reversed(new_events):
                                    msg = format_alert(addr, txt)
                                    try:
                                        await bot_app.bot.send_message(
                                            chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN
                                        )
                                    except Conflict:
                                        logger.warning("Conflict al enviar (doble polling).")
                                    except Exception as e:
                                        logger.error(f"Send failed chat {chat_id}: {e}")
                                STORE.set_last_event_id(chat_id, addr, new_events[0][0])

                    STORE.set_last_check(chat_id)
                except Exception as e:
                    logger.error(f"Error monitoreando {addr}: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.exception(f"Loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# =========================
# HTTP server (healthcheck)
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
# Error handler
# =========================
async def error_handler(update: object, context: CallbackContext) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning("Conflict detectado (otro getUpdates activo).")
        return
    logger.exception(f"Excepción no manejada: {err}")

# =========================
# Pre-start cleanup
# =========================
async def pre_start_cleanup(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook eliminado (si existía).")
    except Exception as e:
        logger.warning(f"No se pudo eliminar webhook (puede no existir): {e}")

# =========================
# Async main (Python 3.13 safe)
# =========================
async def async_main():
    # Lanza el HTTP server en un hilo aparte
    threading.Thread(target=run_http_server, daemon=True).start()

    # Construir app de Telegram
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("walletsus", subs_cmd))
    app.add_handler(CommandHandler("status", stat_cmd))
    app.add_handler(CommandHandler("checknow", checknow_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_error_handler(error_handler)

    # Limpia webhook
    await pre_start_cleanup(app)

    # Arranca monitor como tarea asíncrona
    asyncio.create_task(monitor_loop(app))

    # Secuencia recomendada por PTB (async)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    logger.info("Bot iniciado (polling + HTTP healthcheck, async).")

    # Espera señales y mantiene vivo el loop
    await app.updater.wait()  # equivalente a idle()

    # Shutdown ordenado
    await app.stop()
    await app.shutdown()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
