# app.py
import os
import re
import time
import json
import html
import asyncio
import logging
import datetime as dt
from typing import Optional, Dict, Any, List, Tuple

import httpx
from bs4 import BeautifulSoup

# --- Telegram ---
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Storage (Redis opcional) ---
REDIS_URL = os.getenv("REDIS_URL", "")
USE_REDIS = bool(REDIS_URL)
if USE_REDIS:
    import redis  # type: ignore

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en variables de entorno.")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; WalletWatchBot/1.0)")

HYPERBOT_BASE = "https://hyperbot.network"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hyperbot-telegram")

# --------- Persistencia ---------
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

# --------- Cliente HTTP ---------
def client() -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers=headers, follow_redirects=True)

# --------- Extracción de eventos desde hyperbot.network ----------
class HyperbotExtractor:
    TRADER_PATH = "/trader/{addr}"

    @staticmethod
    def _trader_url(addr: str) -> str:
        return f"{HYPERBOT_BASE}{HyperbotExtractor.TRADER_PATH.format(addr=addr)}"

    @staticmethod
    def _normalize_addr(addr: str) -> str:
        addr = addr.strip()
        return addr.lower()

    @staticmethod
    def _parse_next_data(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        # Busca JSON embebido típico de Next.js: <script id="__NEXT_DATA__" type="application/json">...</script>
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
        # fallback: extrae textos de “cards”/items que suelen listar actividades
        # Captura spans/divs que contengan palabras clave típicas
        keywords = ["Perp", "Trade", "Order", "Opened", "Closed", "Created", "Filled", "Whales", "Recent", "Position"]
        texts: List[str] = []
        for el in soup.find_all(text=True):
            t = " ".join(el.strip().split())
            if len(t) < 4:
                continue
            if any(k.lower() in t.lower() for k in keywords):
                texts.append(t)
        # Limpiar duplicados preservando orden
        seen = set()
        uniq = []
        for t in texts:
            if t not in seen:
                uniq.append(t)
                seen.add(t)
        return uniq[:500]  # cota de seguridad

    @staticmethod
    def _build_event_id(s: str) -> str:
        # ID determinístico desde el texto del evento
        return str(abs(hash(s)) % (10**16))

    @staticmethod
    def _format_event_text(raw: str) -> str:
        # Formateo mínimo: números, pares, size, etc.
        # Resalta “Valor de posición” si aparece
        raw = html.unescape(raw)
        raw = re.sub(r"\s+", " ", raw).strip()

        # Negritas para "Valor de posición"
        raw = re.sub(r"(Valor de posición\s*=\s*\$?[0-9\.,]+)", r"💰 *\1*", raw, flags=re.IGNORECASE)

        # Emojis para buy/sell/open/close
        raw = re.sub(r"\b(Buy|Long|Opened|Open)\b", r"🟢 \1", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(Sell|Short|Closed|Close)\b", r"🔴 \1", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\b(Order|Trade|Filled|Fill)\b", r"📄 \1", raw, flags=re.IGNORECASE)

        return raw

    @classmethod
    async def fetch_events(cls, addr: str) -> List[Tuple[str, str]]:
        """
        Devuelve una lista de (event_id, event_text) en orden cronológico aproximado.
        Estrategia:
          1) Intentar parsear __NEXT_DATA__ si existe.
          2) Si no, heurística sobre el HTML (líneas relevantes).
        """
        addr = cls._normalize_addr(addr)
        url = cls._trader_url(addr)
        async with client() as c:
            resp = await c.get(url)
            resp.raise_for_status()
            html_text = resp.text

        soup = BeautifulSoup(html_text, "html.parser")

        # 1) Next.js JSON embebido
        events: List[Tuple[str, str]] = []
        next_data = cls._parse_next_data(soup)
        if next_data:
            # Busca dentro del árbol posibles listas de eventos
            # Claves típicas: "props", "pageProps", "events", "activities", etc.
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
            # Si encontró, formatea
            if tmp:
                for eid, raw in tmp:
                    events.append((eid, cls._format_event_text(raw)))

        # 2) Heurística por HTML (fallback o complemento)
        if not events:
            lines = cls._heuristic_event_lines(soup)
            # Reduce ruido juntando líneas contiguas de un mismo "card"
            # Simple: agrupa por punto final o •
            combined: List[str] = []
            buf = ""
            for t in lines:
                if buf:
                    buf += " | " + t
                else:
                    buf = t
                if t.endswith(".") or "•" in t or len(buf) > 220:
                    combined.append(buf)
                    buf = ""
            if buf:
                combined.append(buf)
            # Crear ids y formatear
            for raw in combined:
                eid = cls._build_event_id(raw)
                events.append((eid, cls._format_event_text(raw)))

        # Quitar duplicados por event_id y truncar
        seen = set()
        dedup: List[Tuple[str, str]] = []
        for eid, txt in events:
            if eid not in seen:
                dedup.append((eid, txt))
                seen.add(eid)

        # Heurística: orden aproximado por id (no hay timestamp); mantenemos como vienen
        return dedup[:100]


# --------- Formateo de mensajes ----------
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
        "Recibirás alertas cuando detecte *órdenes nuevas, aperturas/cierres de posiciones, trades ejecutados* u otros movimientos relevantes visibles en la página pública de Hyperbot.\n"
        "Tip: ajusta el intervalo con `POLL_INTERVAL_SECONDS` en variables de entorno si quieres más/menos frecuencia."
    )

# --------- Handlers de Telegram ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Abrir Hyperbot", url="https://hyperbot.network")]]
    )
    await update.message.reply_text(format_help(), reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    last = STORE.get_last_check(chat_id)
    if wal:
        t = dt.datetime.utcfromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S UTC") if last else "—"
        await update.message.reply_text(
            f"👛 Wallet actual: `{wal}`\nÚltimo chequeo: {t}", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("No tienes una wallet suscrita. Usa `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wal = STORE.get_wallet(chat_id)
    if wal:
        STORE.set_wallet(chat_id, "")
        await update.message.reply_text("Se ha detenido el monitoreo. Puedes suscribirte de nuevo con `/walletsus <direccion>`.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("No había monitoreo activo.", parse_mode=ParseMode.MARKDOWN)

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

async def subs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: `/walletsus <direccion>`", parse_mode=ParseMode.MARKDOWN)
        return
    addr = context.args[0].strip()
    if not ADDR_RE.match(addr):
        await update.message.reply_text("Dirección inválida. Debe ser un address tipo `0x...` de 42 chars.", parse_mode=ParseMode.MARKDOWN)
        return
    STORE.set_wallet(chat_id, addr)
    # Limpiar último evento para forzar notificaciones nuevas
    STORE.set_last_event_id(chat_id, addr, "")
    await update.message.reply_text(
        f"✅ Monitoreando la wallet:\n`{addr}`\n\nTe enviaré alertas cuando detecte actividad en Hyperbot.",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

# --------- Loop de monitoreo ----------
async def monitor_loop(app):
    await asyncio.sleep(2)
    extractor = HyperbotExtractor()
    while True:
        try:
            # Obtener todos los chats con wallet suscrita.
            # Si Redis: escanear keys; si memoria: iterar.
            chats_wallets: List[Tuple[int, str]] = []
            if USE_REDIS:
                # Keys tipo chat:<id>:wallet
                for key in app.bot_data.get("redis_keys_cache", []):
                    pass  # no-op
                # Escaneo dinámico
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
                # Memoria local
                for k, v in STORE.memory.items():
                    if k.endswith(":wallet") and v:
                        chat_id = int(k.split(":")[1])
                        chats_wallets.append((chat_id, v))

            if not chats_wallets:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            async with httpx.AsyncClient() as _:
                pass  # mantener loop simple

            for chat_id, addr in chats_wallets:
                try:
                    events = await extractor.fetch_events(addr)
                    last_id = STORE.get_last_event_id(chat_id, addr) or ""
                    # Si last_id vacío, solo marca el más reciente sin spamear
                    if not last_id and events:
                        STORE.set_last_event_id(chat_id, addr, events[0][0])
                        STORE.set_last_check(chat_id)
                        continue

                    new_events: List[Tuple[str, str]] = []
                    seen = (last_id != "")
                    for eid, text in events:
                        if seen:
                            # Ya pasamos el último visto: desde aquí son nuevos
                            new_events.append((eid, text))
                        if eid == last_id:
                            seen = True

                    # Si no se encontró last_id en events, por seguridad toma últimos 1-3 para no spamear
                    if last_id and not new_events and events:
                        new_events = events[:1]

                    # Enviar nuevos, del más antiguo al más nuevo (reversa si hace falta)
                    if new_events:
                        for eid, txt in reversed(new_events):
                            msg = format_alert(addr, txt)
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=False
                                )
                            except Exception as e:
                                logger.warning(f"Send failed chat {chat_id}: {e}")
                        # Actualiza último visto al más reciente
                        STORE.set_last_event_id(chat_id, addr, new_events[0][0])

                    STORE.set_last_check(chat_id)
                except Exception as e:
                    logger.exception(f"Error monitoreando {addr} para {chat_id}: {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except Exception as e:
            logger.exception(f"Monitor loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# --------- Bootstrap ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("walletsus", subs_cmd))
    app.add_handler(CommandHandler("status", stat_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))

    # Tarea de fondo
    app.job_queue.run_once(lambda *_: None, 0)  # nudge
    app.post_init(lambda a: asyncio.create_task(monitor_loop(a)))

    logger.info("Bot iniciado.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
