# app.py — Telegram + FastAPI en modo Webhook (Python 3.13 safe)
import os
import re
import io
import csv
import json
import html
import time
import httpx
import uvicorn
import logging
import asyncio
import hashlib
import datetime as dt
import threading
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ContextTypes, CallbackContext
)

# ----------------------- Config -----------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hyperbot-webhook")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL")  # ej: https://tu-servicio.onrender.com
if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN")
if not PUBLIC_URL:
    raise RuntimeError("Falta PUBLIC_URL (URL pública de Render)")

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; WalletWatchBot/1.0)")
HYPERBOT_BASE = "https://hyperbot.network"
PAGE_CHANGE_ALERTS = os.getenv("PAGE_CHANGE_ALERTS", "false").lower() in ("1","true","yes")

REDIS_URL = os.getenv("REDIS_URL", "")
USE_REDIS = bool(REDIS_URL)
if USE_REDIS:
    import redis

# ----------------------- Persistencia -----------------------
class Storage:
    def __init__(self):
        self.memory: Dict[str, str] = {}
        self.last_event: Dict[str, str] = {}
        self.last_check: Dict[str, float] = {}
        self.page_hash: Dict[str, str] = {}
        self.r = redis.from_url(REDIS_URL, decode_responses=True) if USE_REDIS else None

    def _k(self, chat_id: int, suffix: str) -> str:
        return f"chat:{chat_id}:{suffix}"

    def set_wallet(self, chat_id: int, addr: str) -> None:
        k = self._k(chat_id,"wallet")
        (self.r.set(k, addr) if self.r else self.memory.__setitem__(k, addr))

    def get_wallet(self, chat_id: int) -> Optional[str]:
        k = self._k(chat_id,"wallet")
        return self.r.get(k) if self.r else self.memory.get(k)

    def set_last_event_id(self, chat_id: int, wallet: str, event_id: str) -> None:
        k = self._k(chat_id, f"last_event:{wallet.lower()}")
        (self.r.set(k, event_id) if self.r else self.last_event.__setitem__(k, event_id))

    def get_last_event_id(self, chat_id: int, wallet: str) -> Optional[str]:
        k = self._k(chat_id, f"last_event:{wallet.lower()}")
        return self.r.get(k) if self.r else self.last_event.get(k)

    def set_last_check(self, chat_id: int) -> None:
        k = self._k(chat_id, "last_check_ts")
        ts = time.time()
        (self.r.set(k, str(ts)) if self.r else self.last_check.__setitem__(k, ts))

    def get_last_check(self, chat_id: int) -> Optional[float]:
        k = self._k(chat_id, "last_check_ts")
        if self.r:
            v = self.r.get(k); return float(v) if v else None
        return self.last_check.get(k)

    def set_page_hash(self, chat_id: int, wallet: str, h: str) -> None:
        k = self._k(chat_id, f"page_hash:{wallet.lower()}")
        (self.r.set(k, h) if self.r else self.page_hash.__setitem__(k, h))

    def get_page_hash(self, chat_id: int, wallet: str) -> Optional[str]:
        k = self._k(chat_id, f"page_hash:{wallet.lower()}")
        return self.r.get(k) if self.r else self.page_hash.get(k)

STORE = Storage()

# ----------------------- HTTP client -----------------------
def http_client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    return httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                             headers=headers, follow_redirects=True)

# ----------------------- Utilidades -----------------------
def _bold_value_position(txt: str) -> str:
    return re.sub(r"(Valor de posición\s*=\s*\$?[0-9\.,]+)", r"💰 *\1*", txt, flags=re.IGNORECASE)

def _format_line(raw: str) -> str:
    raw = html.unescape(raw)
    raw = re.sub(r"\s+"," ", raw).strip()
    raw = _bold_value_position(raw)
    raw = re.sub(r"\b(Buy|Long|Opened|Open)\b", r"🟢 \1", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(Sell|Short|Closed|Close)\b", r"🔴 \1", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\b(Order|Trade|Filled|Fill)\b", r"📄 \1", raw, flags=re.IGNORECASE)
    return raw

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8","ignore")).hexdigest()[:16]

def _utc_now() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

# ----------------------- Extractor Hyperbot -----------------------
class HyperbotExtractor:
    @staticmethod
    def url(addr: str) -> str:
        return f"{HYPERBOT_BASE}/trader/{addr.strip().lower()}"

    @staticmethod
    async def fetch_events(addr: str) -> Tuple[List[Tuple[str,str]], Dict[str,Any]]:
        url = HyperbotExtractor.url(addr)
        meta: Dict[str, Any] = {"reason":"", "page_hash":"", "discovered_urls":[]}
        async with http_client() as c:
            r = await c.get(url); r.raise_for_status()
            html_text = r.text

        meta["page_hash"] = _hash(html_text)
        soup = BeautifulSoup(html_text, "html.parser")
        events_texts: List[str] = []

        # 1) __NEXT_DATA__
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag:
            try:
                data = json.loads(tag.text)
                def walk(node):
                    if isinstance(node, dict):
                        for k,v in node.items():
                            if k.lower() in ("events","activities","activity","trades","orders","recent","positions","fills"):
                                try:
                                    for it in v: events_texts.append(json.dumps(it, ensure_ascii=False))
                                except Exception: pass
                            walk(v)
                    elif isinstance(node, list):
                        for it in node: walk(it)
                walk(data)
            except Exception:
                pass

        # 2) Descubrimiento de endpoints JSON/CSV en el HTML
        if not events_texts:
            addr_l = addr.lower()
            url_re = re.compile(r"""(?P<u>https?://[^\s"'<>]+|/[A-Za-z0-9_\-\/\.\?\=&%]+)""")
            candidates = []
            for m in url_re.finditer(html_text):
                u = m.group("u")
                if addr_l in u.lower() and ("/api/" in u.lower() or u.lower().endswith((".json",".csv"))):
                    candidates.append(u)
            # dedup
            seen=set(); disc=[]
            for u in candidates:
                if u not in seen:
                    disc.append(u); seen.add(u)
            meta["discovered_urls"] = disc[:10]

            async with http_client() as c:
                for u in meta["discovered_urls"]:
                    if not (u.startswith("http://") or u.startswith("https://")):
                        u = HYPERBOT_BASE.rstrip("/") + "/" + u.lstrip("/")
                    try:
                        r = await c.get(u)
                        if r.status_code != 200: continue
                        ct = r.headers.get("content-type","")
                        if "application/json" in ct or r.text.strip().startswith(("{","[")):
                            try:
                                obj = r.json()
                                # parse generic JSON
                                def walk(o):
                                    if isinstance(o, dict):
                                        keys = {k.lower() for k in o.keys()}
                                        if {"symbol","action","size"} <= keys or {"pair","side","size"} <= keys:
                                            sym=o.get("symbol") or o.get("pair") or ""
                                            act=o.get("action") or o.get("side") or ""
                                            size=o.get("size") or o.get("qty") or o.get("amount") or ""
                                            price=o.get("price") or o.get("entry") or ""
                                            pnl=o.get("pnl") or o.get("roe") or ""
                                            events_texts.append(_format_line(f"{sym} {act} size={size} price={price} pnl={pnl}"))
                                        for v in o.values(): walk(v)
                                    elif isinstance(o, list):
                                        for it in o: walk(it)
                                walk(obj)
                            except Exception:
                                pass
                        elif "text/csv" in ct or u.lower().endswith(".csv"):
                            try:
                                f = io.StringIO(r.text); rd = csv.DictReader(f)
                                for row in rd:
                                    sym=row.get("Symbol") or row.get("symbol") or row.get("pair") or ""
                                    act=row.get("Action") or row.get("action") or ""
                                    size=row.get("Size") or row.get("size") or ""
                                    price=row.get("Price") or row.get("price") or ""
                                    pnl=row.get("Closed PnL") or row.get("pnl") or row.get("ROE") or ""
                                    events_texts.append(_format_line(f"{sym} {act} size={size} price={price} pnl={pnl}"))
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning(f"Fallo endpoint descubierto: {e}")

        # 3) Fallback heurístico de texto visible
        if not events_texts:
            keywords = ["perp","trade","order","opened","closed","created","filled","recent","position","entry","liq","value"]
            texts=[]
            for el in soup.find_all(string=True):
                t=" ".join(el.strip().split())
                if len(t)>=4 and any(k in t.lower() for k in keywords):
                    texts.append(t)
            buf=""; combined=[]
            for t in texts:
                buf = (buf + " | " + t) if buf else t
                if t.endswith(".") or "•" in t or len(buf)>200:
                    combined.append(buf); buf=""
            if buf: combined.append(buf)
            events_texts.extend(_format_line(x) for x in combined)

        # build events (id,text)
        events=[]
        seen=set()
        for raw in events_texts:
            eid = str(abs(hash(raw)) % (10**16))
            if eid in seen: continue
            seen.add(eid)
            events.append((eid, raw))

        meta["reason"] = "ok" if events else "no_events_found"
        return events[:100], meta

# ----------------------- Mensajes -----------------------
def format_alert(addr: str, event_text: str) -> str:
    link = f"{HYPERBOT_BASE}/trader/{addr.lower()}"
    ts = _utc_now()
    return (f"🔔 *Actividad detectada*\n"
            f"👛 Wallet: `{addr}`\n"
            f"{event_text}\n"
            f"⏱ {ts}\n"
            f"[Ver en Hyperbot]({link})")

def help_text() -> str:
    return ("Hola 👋\n\n"
            "*Comandos:*\n"
            "• `/walletsus <direccion>` — Suscribirte (reemplaza la anterior)\n"
            "• `/status` — Wallet suscrita y último chequeo\n"
            "• `/checknow` — Forzar snapshot inmediato\n"
            "• `/stop` — Detener monitoreo\n\n"
            "_Ejemplo:_ `/walletsus 0xc2a30212a8ddac9e123944d6e29faddce994e5f2`")

# ----------------------- Handlers Telegram -----------------------
ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Abrir Hyperbot", url="https://hyperbot.network")]])
    await update.message.reply_text(help_text(), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    w = STORE.get_wallet(chat_id)
    last = STORE.get_last_check(chat_id)
    t = dt.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S UTC") if last else "—"
    if w:
        await update.message.reply_text(f"👛 Wallet actual: `{w}`\nÚltimo chequeo: {t}", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No tienes una wallet suscrita. Usa `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    STORE.set_wallet(chat_id, "")
    await update.message.reply_text("✅ Monitoreo detenido. Suscríbete nuevamente con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: `/walletsus <direccion>`", parse_mode=ParseMode.MARKDOWN)
        return
    addr = context.args[0].strip()
    if not ADDR_RE.match(addr):
        await update.message.reply_text("Dirección inválida. Debe empezar con `0x` y tener 42 caracteres.", parse_mode=ParseMode.MARKDOWN)
        return
    STORE.set_wallet(chat_id, addr)
    STORE.set_last_event_id(chat_id, addr, "")
    await update.message.reply_text(f"✅ Monitoreando la wallet:\n`{addr}`\n\nTe enviaré alertas cuando detecte actividad.", parse_mode=ParseMode.MARKDOWN)

async def cmd_checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    if not wal:
        await update.message.reply_text("Primero suscríbete con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN); return
    await update.message.reply_text("⏳ Checando ahora mismo en Hyperbot...", parse_mode=ParseMode.MARKDOWN)
    events, meta = await HyperbotExtractor.fetch_events(wal)
    if events:
        sample = "\n".join(f"• {txt}" for _, txt in events[:3])
        await update.message.reply_text(f"✅ Encontré {len(events)} posibles eventos (muestra):\n{sample}\n\nHash página: `{meta.get('page_hash','')}`",
                                        parse_mode=ParseMode.MARKDOWN)
    else:
        msg = (f"⚠️ *No encontré eventos estructurados.*\n"
               f"Motivo: `{meta.get('reason','')}`\nHash página: `{meta.get('page_hash','')}`\n")
        if meta.get("discovered_urls"):
            msg += "Posibles endpoints:\n" + "\n".join("• " + u for u in meta["discovered_urls"])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ----------------------- Monitor Hyperbot -----------------------
async def monitor_loop(app: Application):
    await asyncio.sleep(2)
    while True:
        try:
            # Recopila suscripciones
            chats_wallets: List[Tuple[int,str]] = []
            if USE_REDIS:
                cursor = 0
                while True:
                    cursor, keys = STORE.r.scan(cursor=cursor, match="chat:*:wallet", count=100)
                    for k in keys:
                        addr = STORE.r.get(k) or ""
                        if addr:
                            chat_id = int(k.split(":")[1]); chats_wallets.append((chat_id, addr))
                    if cursor == 0: break
            else:
                for k,v in list(STORE.memory.items()):
                    if k.endswith(":wallet") and v:
                        chat_id = int(k.split(":")[1]); chats_wallets.append((chat_id, v))

            if not chats_wallets:
                await asyncio.sleep(POLL_INTERVAL_SECONDS); continue

            for chat_id, addr in chats_wallets:
                try:
                    events, meta = await HyperbotExtractor.fetch_events(addr)
                    last_id = STORE.get_last_event_id(chat_id, addr) or ""
                    prev_hash = STORE.get_page_hash(chat_id, addr)
                    STORE.set_page_hash(chat_id, addr, meta.get("page_hash",""))

                    if not events and PAGE_CHANGE_ALERTS and prev_hash and prev_hash != meta.get("page_hash"):
                        try:
                            await app.bot.send_message(chat_id=chat_id,
                                text=(f"ℹ️ *La página de Hyperbot cambió para*\n`{addr}`\n"
                                      f"_Puede haber actividad nueva no legible por el parser._\n{_utc_now()}"),
                                parse_mode=ParseMode.MARKDOWN)
                        except Exception as e:
                            logger.warning(f"Page-change send fail: {e}")

                    if events:
                        new_events=[]
                        if not last_id:
                            STORE.set_last_event_id(chat_id, addr, events[0][0])
                        else:
                            for eid, txt in events:
                                if eid == last_id: break
                                new_events.append((eid, txt))
                            if new_events:
                                for eid, txt in reversed(new_events):
                                    try:
                                        await app.bot.send_message(chat_id=chat_id,
                                            text=format_alert(addr, txt), parse_mode=ParseMode.MARKDOWN)
                                    except Conflict:
                                        logger.warning("Conflict al enviar (doble instancia?).")
                                    except Exception as e:
                                        logger.error(f"Send failed: {e}")
                                STORE.set_last_event_id(chat_id, addr, new_events[0][0])

                    STORE.set_last_check(chat_id)
                except Exception as e:
                    logger.error(f"Monitor error {addr}: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.exception(f"Loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ----------------------- FastAPI (Webhook & Health) -----------------------
app_http = FastAPI()

@app_http.get("/")
def root():
    return {"ok": True, "service": "hyperbot-network-wallet-tracking-bot", "mode": "webhook"}

@app_http.get("/healthz")
def healthz():
    return {"status": "healthy"}

# Telegram Webhook endpoint (se crea dinámicamente con el token)
# Nota: no valida el token en ruta (ya es suficientemente impredecible), pero podrías añadir un secreto extra si gustas.
@app_http.post(f"/telegram/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    body = await req.json()
    update = Update.de_json(body, bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

# ----------------------- Arranque asíncrono -----------------------
async def setup_and_run():
    global telegram_app
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("walletsus", cmd_subs))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("checknow", cmd_checknow))
    telegram_app.add_handler(CommandHandler("stop", cmd_stop))

    # Inicializa bot
    await telegram_app.initialize()

    # Configura webhook en Telegram
    webhook_url = f"{PUBLIC_URL.rstrip('/')}/telegram/{BOT_TOKEN}"
    # Elimina polling/webhook previos y registra el nuevo
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await telegram_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook configurado: {webhook_url}")

    # Inicia la aplicación de PTB (para poder enviar mensajes, timers, etc.)
    await telegram_app.start()

    # Lanza el monitor en segundo plano
    asyncio.create_task(monitor_loop(telegram_app))

def run_http():
    port = int(os.environ.get("PORT","10000"))
    # Uvicorn corre en ESTE mismo event loop porque lo lanzamos con 'asyncio.create_task' más abajo? Mejor en hilo aparte:
    uvicorn.run(app_http, host="0.0.0.0", port=port, log_level="warning")

def main():
    # 1) Arranca FastAPI (HTTP) en hilo separado
    threading.Thread(target=run_http, daemon=True).start()
    # 2) Arranca la app de Telegram + registra webhook en el loop principal
    asyncio.run(setup_and_run())
    # 3) Mantén el proceso vivo (el HTTP server queda en otro hilo; aquí dormimos)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
