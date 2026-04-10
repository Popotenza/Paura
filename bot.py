"""
╔══════════════════════════════════════════════════════════════════╗
║               TELEGRAM MASTER USERBOT — bot.py                  ║
║  Controlla tutti gli slave via HTTP dai tuoi Messaggi Salvati.  ║
╚══════════════════════════════════════════════════════════════════╝

VARIABILI D'AMBIENTE RICHIESTE
───────────────────────────────
  API_ID          → API ID del tuo account Telegram (da my.telegram.org)
  API_HASH        → API Hash del tuo account Telegram
  SESSION_STRING  → Session string (lascia vuota al primo avvio)

COMANDI RAPIDI
──────────────
  /h  → aiuto completo con tutti i comandi
  /s  → stato attuale del bot
  /on → avvia  |  /off → ferma
"""

import asyncio
import json
import logging
import os
import urllib.parse

from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.tl.functions.messages import GetDialogFiltersRequest


# ═══════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# COSTANTI
# ═══════════════════════════════════════════════════════════════════

CONFIG_FILE       = os.path.join(os.path.dirname(__file__), "config.json")
SLAVE_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "slave_config.json")

trigger_now = asyncio.Event()
COLOR_MAP   = {"#g": "🟢", "#r": "🔴", "#p": "🔵"}

# ── Testi riutilizzabili ───────────────────────────────────────────
SEP  = "─" * 30          # separatore corto
SEPL = "━" * 30          # separatore lungo


# ═══════════════════════════════════════════════════════════════════
# GESTIONE CONFIGURAZIONE
# ═══════════════════════════════════════════════════════════════════

def load_config() -> dict:
    defaults = {
        "sources":          [],
        "targets":          [],
        "interval":         10,
        "slave_intervals":  {},
        "slave_sources":    {},
        "slave_addlists":   {},
        "global_addlists":  [],
        "last_ids":         {},
        "rotation_indices": {},
        "running":          True,
        "buttons_rows":     [],
        "auto_reply_text":  "",
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for key, value in defaults.items():
            cfg.setdefault(key, value)
        return cfg
    return defaults


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


async def update_slave_config(client, cfg: dict) -> None:
    async def resolve(peer_id):
        try:
            entity = await client.get_entity(peer_id)
            if username := getattr(entity, "username", None):
                return f"@{username}"
        except Exception:
            pass
        return peer_id

    sources = [await resolve(s) for s in cfg.get("sources", [])]
    targets = [await resolve(t) for t in cfg.get("targets", [])]

    slave_sources = {}
    for slave_n, peer_ids in cfg.get("slave_sources", {}).items():
        slave_sources[slave_n] = [await resolve(p) for p in peer_ids]

    slave_cfg = {
        "sources":         sources,
        "targets":         targets,
        "buttons_rows":    cfg.get("buttons_rows", []),
        "interval":        cfg.get("interval", 10),
        "slave_intervals": cfg.get("slave_intervals", {}),
        "slave_sources":   slave_sources,
        "slave_addlists":  cfg.get("slave_addlists", {}),
        "global_addlists": cfg.get("global_addlists", []),
        "running":         cfg.get("running", True),
        "auto_reply_text": cfg.get("auto_reply_text", ""),
    }

    with open(SLAVE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(slave_cfg, f, ensure_ascii=False, indent=4)

    logger.info("🔄 slave_config.json aggiornato")


# ═══════════════════════════════════════════════════════════════════
# BOTTONI INLINE
# ═══════════════════════════════════════════════════════════════════

def _apply_color(text: str) -> str:
    for prefix, emoji in COLOR_MAP.items():
        if text.lower().startswith(prefix + " "):
            return emoji + " " + text[len(prefix):].strip()
        if text.lower().startswith(prefix):
            return emoji + text[len(prefix):]
    return text


def _make_url(raw: str) -> str:
    raw = raw.strip()
    if raw.lower().startswith("share:"):
        encoded = urllib.parse.quote(raw[6:].strip(), safe="")
        return f"https://t.me/share/url?text={encoded}"
    return raw


def parse_buttons(definition: str) -> list[list[dict]]:
    rows = []
    for line in definition.strip().splitlines():
        row = []
        for part in line.strip().split("&&"):
            part = part.strip()
            if " - " not in part:
                continue
            label, url = part.split(" - ", 1)
            btn = {"text": _apply_color(label.strip()), "url": _make_url(url.strip())}
            if btn["text"] and btn["url"]:
                row.append(btn)
        if row:
            rows.append(row)
    return rows


def build_buttons(cfg: dict) -> list | None:
    rows = cfg.get("buttons_rows", [])
    if not rows:
        return None
    return [[Button.url(b["text"], b["url"]) for b in row] for row in rows]


def format_buttons_preview(rows: list[list[dict]]) -> str:
    if not rows:
        return "  Nessun bottone configurato."
    return "\n".join(
        "  " + "  |  ".join(f"[ {b['text']} ]" for b in row)
        for row in rows
    )


# ═══════════════════════════════════════════════════════════════════
# CARTELLE TELEGRAM
# ═══════════════════════════════════════════════════════════════════

def get_folder_title(folder) -> str:
    title = folder.title
    if isinstance(title, str):
        return title
    if hasattr(title, "text"):
        return title.text
    return str(title)


async def get_folders(client) -> list:
    result = await client(GetDialogFiltersRequest())
    return [f for f in result.filters if hasattr(f, "include_peers")]


async def resolve_folder_peers(client, folder) -> list[tuple]:
    peers = []
    for peer in folder.include_peers:
        try:
            entity  = await client.get_entity(peer)
            peer_id = get_peer_id(entity)
            name    = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
            peers.append((peer_id, name))
        except Exception as e:
            logger.warning(f"Impossibile risolvere peer {peer}: {e}")
    return peers


async def add_folder_to_list(client, event, folder_name: str, is_source: bool) -> None:
    global config

    folders = await get_folders(client)
    matched = next((f for f in folders if get_folder_title(f).lower() == folder_name.lower()), None)

    if not matched:
        available = "\n".join(f"  • {get_folder_title(f)}" for f in folders) or "  Nessuna cartella trovata"
        await event.reply(
            f"❌ **Cartella non trovata:** `{folder_name}`\n\n"
            f"📁 **Cartelle disponibili:**\n{available}\n\n"
            f"_Usa il nome esatto, rispettando maiuscole/minuscole._"
        )
        return

    peers = await resolve_folder_peers(client, matched)
    if not peers:
        await event.reply("⚠️ La cartella è vuota o i peer non sono risolvibili.")
        return

    key  = "sources" if is_source else "targets"
    tipo = "sorgenti" if is_source else "destinazioni"
    added, skipped = [], []

    for peer_id, name in peers:
        if peer_id not in config[key]:
            config[key].append(peer_id)
            added.append(name)
        else:
            skipped.append(name)

    save_config(config)
    await update_slave_config(client, config)

    title  = get_folder_title(matched)
    msg    = f"📁 **{title}**\n{SEP}\n"
    if added:
        msg += f"✅ Aggiunti come {tipo} ({len(added)}):\n"
        msg += "\n".join(f"  • {n}" for n in added) + "\n"
    if skipped:
        msg += f"\n⏭ Già presenti ({len(skipped)}):\n"
        msg += "\n".join(f"  • {n}" for n in skipped)

    await event.reply(msg)


# ═══════════════════════════════════════════════════════════════════
# INVIO MESSAGGI
# ═══════════════════════════════════════════════════════════════════

async def copy_to_target(client, msg, target, cfg: dict, _retries: int = 0) -> None:
    try:
        text     = msg.message or getattr(msg, "caption", "") or ""
        entities = msg.entities or []
        buttons  = build_buttons(cfg)

        if msg.media:
            try:
                await client.send_file(
                    target, file=msg.media, caption=text,
                    formatting_entities=entities, buttons=buttons, silent=False,
                )
            except Exception as media_err:
                logger.warning(f"Media fallito su {target} ({media_err}) — invio solo testo")
                if text:
                    await client.send_message(target, text, formatting_entities=entities, buttons=buttons)
        else:
            await client.send_message(target, text, formatting_entities=entities, buttons=buttons)

        logger.info(f"✅ msg {msg.id} → {target}")

    except FloodWaitError as e:
        if _retries >= 3:
            logger.error(f"FloodWait ripetuto ({_retries}x) su {target} — messaggio saltato.")
            return
        logger.warning(f"FloodWait {e.seconds}s (tentativo {_retries + 1}/3)")
        await asyncio.sleep(e.seconds + 1)
        await copy_to_target(client, msg, target, cfg, _retries + 1)

    except Exception as e:
        logger.error(f"Errore invio → {target}: {e}")


async def send_to_all(client, msg, cfg: dict) -> None:
    if not cfg["targets"]:
        return
    await asyncio.gather(*[copy_to_target(client, msg, t, cfg) for t in cfg["targets"]])


# ═══════════════════════════════════════════════════════════════════
# SPAM LOOP
# ═══════════════════════════════════════════════════════════════════

async def spam_loop(client, cfg: dict) -> None:
    while True:
        if not cfg["running"]:
            await asyncio.sleep(5)
            continue

        try:
            await asyncio.wait_for(trigger_now.wait(), timeout=cfg["interval"] * 60)
            trigger_now.clear()
        except asyncio.TimeoutError:
            pass

        if not cfg["running"]:
            continue

        updated = False
        for source in cfg["sources"][:]:
            try:
                all_msgs = await client.get_messages(source, limit=200)
                valid    = sorted([m for m in all_msgs if m.message or m.media], key=lambda m: m.id)

                if not valid:
                    logger.info(f"Nessun post valido in {source}")
                    continue

                key = str(source)
                idx = cfg.setdefault("rotation_indices", {}).get(key, 0) % len(valid)
                msg = valid[idx]

                logger.info(f"📤 Post {idx + 1}/{len(valid)} (id={msg.id}) da {source}")
                await send_to_all(client, msg, cfg)

                cfg["rotation_indices"][key] = (idx + 1) % len(valid)
                updated = True

            except Exception as e:
                logger.error(f"Errore sorgente {source}: {e}")

        if updated:
            save_config(cfg)


# ═══════════════════════════════════════════════════════════════════
# AGGIUNTA ENTITÀ SINGOLA
# ═══════════════════════════════════════════════════════════════════

async def add_entity(client, event, link: str, is_source: bool) -> None:
    global config
    try:
        target  = "me" if link.lower() in ["me", "saved"] else link.strip()
        entity  = await client.get_entity(target)
        peer_id = get_peer_id(entity)
        key     = "sources" if is_source else "targets"
        label   = "🟢 Sorgente" if is_source else "📤 Destinazione"
        name    = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)

        if peer_id not in config[key]:
            config[key].append(peer_id)
            save_config(config)
            await update_slave_config(client, config)
            total = len(config[key])
            await event.reply(f"✅ **{label} aggiunta**\n{SEP}\n📌 {name}\n_Totale: {total}_")
        else:
            await event.reply(f"⚠️ **Già presente**\n{SEP}\n`{name}` è già nella lista.")

    except Exception as e:
        await event.reply(f"❌ **Impossibile aggiungere**\n{SEP}\n`{e}`")


# ═══════════════════════════════════════════════════════════════════
# HELPERS ADDLIST
# ═══════════════════════════════════════════════════════════════════

def format_addlist(links: list, title: str) -> str:
    if not links:
        return f"_{title}: nessuna addlist._"
    lines = "\n".join(f"  `{i + 1}.` {url}" for i, url in enumerate(links))
    return f"**{title}** ({len(links)}):\n{lines}"


# ═══════════════════════════════════════════════════════════════════
# SERVER HTTP
# ═══════════════════════════════════════════════════════════════════

async def start_http_server() -> None:
    async def handle_slave_config(request):
        if not os.path.exists(SLAVE_CONFIG_FILE):
            body = json.dumps({"error": "Config non ancora generata"})
            return web.Response(status=503, text=body, content_type="application/json")
        with open(SLAVE_CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="application/json")

    async def handle_health(request):
        return web.Response(text=json.dumps({"status": "ok"}), content_type="application/json")

    port = int(os.environ.get("PORT", 8080))
    app  = web.Application()
    app.router.add_get("/api/slave-config", handle_slave_config)
    app.router.add_get("/healthz",          handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 HTTP server avviato su porta {port}")

    while True:
        await asyncio.sleep(3600)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    global config

    api_id_str     = os.environ.get("API_ID")
    api_hash       = os.environ.get("API_HASH")
    session_string = os.environ.get("SESSION_STRING", "")

    if not api_id_str or not api_hash:
        logger.error("❌ Imposta API_ID e API_HASH come variabili d'ambiente!")
        return

    client = TelegramClient(StringSession(session_string), int(api_id_str), api_hash)
    await client.start()

    if not session_string:
        print("\n" + "=" * 60)
        print("✅ Copia questa SESSION_STRING nelle variabili d'ambiente:")
        print(client.session.save())
        print("=" * 60 + "\n")

    config = load_config()
    logger.info(f"Avviato | {len(config['sources'])} sorgenti | {len(config['targets'])} destinazioni")

    if config["running"]:
        trigger_now.set()

    globals()["pending_link"]    = None   # link t.me/ normale in attesa di sorgente/destinazione
    globals()["pending_addlist"] = None   # link t.me/addlist/ in attesa di "globale" o "slave N"

    @client.on(events.NewMessage(chats="me"))
    async def command_handler(event):
        global config
        text = (event.message.text or "").strip()
        if not text:
            return

        # ════════════════════════════════════════
        # CONTROLLO BASE
        # ════════════════════════════════════════

        if text in ("/on", "/start") or text.startswith(("/on ", "/start ")):
            config["running"] = True
            save_config(config)
            await update_slave_config(client, config)
            trigger_now.set()
            await event.reply(
                "▶️ **Bot avviato**\n"
                f"{SEP}\n"
                "Il primo post partirà tra pochi secondi.\n"
                f"⏰ Intervallo attivo: **{config['interval']} min**\n\n"
                "_Usa /off per fermare, /s per lo stato._"
            )

        elif text in ("/off", "/stop") or text.startswith(("/off ", "/stop ")):
            config["running"] = False
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                "⏹ **Bot fermato**\n"
                f"{SEP}\n"
                "Nessun post verrà inviato finché non mandi /on."
            )

        elif text == "/s":
            stato      = "🟢 **Attivo**" if config["running"] else "🔴 **Fermo**"
            n_src      = len(config["sources"])
            n_tgt      = len(config["targets"])
            n_btn      = sum(len(r) for r in config.get("buttons_rows", []))
            reply_on   = "✅ attiva" if config.get("auto_reply_text") else "❌ non impostata"
            interval   = config["interval"]

            s_int = config.get("slave_intervals", {})
            s_src = config.get("slave_sources", {})
            s_al  = config.get("slave_addlists", {})
            g_al  = config.get("global_addlists", [])

            # Intervalli slave
            if s_int:
                int_lines = "\n".join(f"  • Slave {k}: {v} min" for k, v in sorted(s_int.items(), key=lambda x: int(x[0])))
            else:
                int_lines = f"  tutti al default ({interval} min)"

            # Sorgenti slave
            if s_src:
                src_lines = "\n".join(f"  • Slave {k}: {len(v)} proprie" for k, v in sorted(s_src.items(), key=lambda x: int(x[0])))
            else:
                src_lines = "  tutti usano le sorgenti master"

            # Addlist
            al_total = len(g_al) + sum(len(v) for v in s_al.values())
            if al_total:
                al_lines = f"  globali: {len(g_al)}"
                for k, v in sorted(s_al.items(), key=lambda x: int(x[0])):
                    al_lines += f"\n  slave {k}: {len(v)}"
            else:
                al_lines = "  nessuna"

            await event.reply(
                f"📊 **STATO DEL BOT**\n"
                f"{SEPL}\n"
                f"Stato:          {stato}\n"
                f"Intervallo:     {interval} min\n"
                f"{SEP}\n"
                f"📥 Sorgenti master:   {n_src}\n"
                f"📤 Destinazioni:      {n_tgt}\n"
                f"🔘 Bottoni:           {n_btn}\n"
                f"💬 Auto-risposta PM:  {reply_on}\n"
                f"{SEP}\n"
                f"⏱ Intervalli slave:\n{int_lines}\n"
                f"{SEP}\n"
                f"📥 Sorgenti slave:\n{src_lines}\n"
                f"{SEP}\n"
                f"🔗 Addlist:\n{al_lines}\n"
                f"{SEPL}\n"
                f"_/h per i comandi disponibili_"
            )

        elif text == "/reset":
            config.update({"sources": [], "targets": [], "last_ids": {}, "rotation_indices": {}})
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                "🔄 **Reset completato**\n"
                f"{SEP}\n"
                "Sorgenti, destinazioni e cronologia azzerati.\n\n"
                "_Addlist e bottoni non sono stati toccati._"
            )

        # ════════════════════════════════════════
        # BOTTONI INLINE
        # ════════════════════════════════════════

        elif text == "/b":
            rows    = config.get("buttons_rows", [])
            preview = format_buttons_preview(rows)
            await event.reply(
                f"🔘 **BOTTONI INLINE**\n"
                f"{SEPL}\n"
                f"{preview}\n"
                f"{SEPL}\n"
                f"**Come impostarli:**\n"
                f"Manda un messaggio così:\n\n"
                f"```\n"
                f"/b\n"
                f"Testo - https://link\n"
                f"#g Verde - https://link && #r Rosso - https://link2\n"
                f"Condividi - share:Testo da condividere\n"
                f"```\n\n"
                f"• Ogni **riga** = una riga di bottoni\n"
                f"• `&&` = bottoni **affiancati** sulla stessa riga\n"
                f"• `#g` 🟢  `#r` 🔴  `#p` 🔵 = colori\n"
                f"• `share:testo` = link di condivisione Telegram\n\n"
                f"_/bclear per rimuovere tutti i bottoni_"
            )

        elif text.startswith("/b\n") or (text.startswith("/b ") and len(text) > 3):
            definition = text[2:].strip()
            if not definition:
                await event.reply("⚠️ Definizione vuota. Scrivi solo `/b` per le istruzioni.")
                return
            rows = parse_buttons(definition)
            if not rows:
                await event.reply(
                    "❌ **Nessun bottone valido trovato.**\n"
                    f"{SEP}\n"
                    "Formato corretto: `Testo - https://url`\n"
                    "Manda `/b` per vedere le istruzioni complete."
                )
                return
            config["buttons_rows"] = rows
            save_config(config)
            await update_slave_config(client, config)
            total   = sum(len(r) for r in rows)
            preview = format_buttons_preview(rows)
            await event.reply(
                f"✅ **{total} bottoni salvati** su {len(rows)} righe\n"
                f"{SEPL}\n"
                f"{preview}\n"
                f"{SEP}\n"
                f"_/bclear per rimuoverli tutti_"
            )

        elif text == "/bclear":
            n = sum(len(r) for r in config.get("buttons_rows", []))
            config["buttons_rows"] = []
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(f"🗑 **{n} bottoni rimossi.**\nI messaggi futuri non avranno bottoni.")

        # ════════════════════════════════════════
        # AUTO-RISPOSTA PM SLAVE
        # ════════════════════════════════════════

        elif text.startswith("/replytext\n") or (text.startswith("/replytext ") and len(text) > 11):
            reply_text = text[len("/replytext"):].strip()
            config["auto_reply_text"] = reply_text
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                f"✅ **Testo auto-risposta impostato**\n"
                f"{SEPL}\n"
                f"{reply_text}\n"
                f"{SEPL}\n"
                f"**Segnaposto disponibili:**\n"
                f"  `{{first_name}}` · `{{last_name}}` · `{{full_name}}` · `{{username}}`\n\n"
                f"_/replyclear per rimuoverlo_"
            )

        elif text == "/replytext":
            current = config.get("auto_reply_text", "")
            if current:
                await event.reply(
                    f"📝 **Testo auto-risposta attuale:**\n"
                    f"{SEPL}\n"
                    f"{current}\n"
                    f"{SEP}\n"
                    f"_/replyclear per rimuoverlo_"
                )
            else:
                await event.reply(
                    f"💬 **Auto-risposta PM**\n"
                    f"{SEP}\n"
                    f"Nessun testo impostato.\n\n"
                    f"**Come impostarlo:**\n"
                    f"`/replytext Ciao {{first_name}}, benvenuto!`\n\n"
                    f"**Segnaposto:** `{{first_name}}` `{{last_name}}` `{{full_name}}` `{{username}}`"
                )

        elif text == "/replyshow":
            reply_text = config.get("auto_reply_text", "")
            stato_al   = "✅ Attiva" if reply_text else "❌ Non impostata"
            msg = (
                f"💬 **AUTO-RISPOSTA PM SLAVE**\n"
                f"{SEP}\n"
                f"Stato: {stato_al}\n"
            )
            if reply_text:
                msg += f"\n**Testo:**\n{reply_text}\n"
            msg += f"\n_/rt <testo> per cambiare · /rc per cancellare_"
            await event.reply(msg)

        elif text == "/replyclear" or text == "/rc":
            config["auto_reply_text"] = ""
            save_config(config)
            await update_slave_config(client, config)
            await event.reply("🗑 **Auto-risposta rimossa.**\nGli slave non risponderanno più ai PM.")

        # ── Alias corti per i comandi reply (/rt /rc /rs) ─────────
        # /rt [testo]  →  /replytext [testo]
        # /rs          →  /replyshow
        # /rc          →  /replyclear  (già gestito sopra)

        elif text.startswith("/rt\n") or (text.startswith("/rt ") and len(text) > 3):
            reply_text = text[3:].strip()
            config["auto_reply_text"] = reply_text
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                f"✅ **Testo auto-risposta impostato**\n"
                f"{SEPL}\n"
                f"{reply_text}\n"
                f"{SEPL}\n"
                f"**Segnaposto disponibili:**\n"
                f"  `{{first_name}}` · `{{last_name}}` · `{{full_name}}` · `{{username}}`\n\n"
                f"_/rc per rimuoverlo_"
            )

        elif text == "/rt":
            current = config.get("auto_reply_text", "")
            if current:
                await event.reply(
                    f"📝 **Testo auto-risposta attuale:**\n"
                    f"{SEPL}\n"
                    f"{current}\n"
                    f"{SEP}\n"
                    f"_/rc per rimuoverlo_"
                )
            else:
                await event.reply(
                    f"💬 **Auto-risposta PM**\n"
                    f"{SEP}\n"
                    f"Nessun testo impostato.\n\n"
                    f"**Come impostarlo:**\n"
                    f"`/rt Ciao {{first_name}}, benvenuto!`\n\n"
                    f"**Segnaposto:** `{{first_name}}` `{{last_name}}` `{{full_name}}` `{{username}}`"
                )

        elif text == "/rs":
            reply_text = config.get("auto_reply_text", "")
            stato_al   = "✅ Attiva" if reply_text else "❌ Non impostata"
            msg = (
                f"💬 **AUTO-RISPOSTA PM SLAVE**\n"
                f"{SEP}\n"
                f"Stato: {stato_al}\n"
            )
            if reply_text:
                msg += f"\n**Testo:**\n{reply_text}\n"
            msg += f"\n_/rt <testo> per cambiare · /rc per cancellare_"
            await event.reply(msg)

        # ════════════════════════════════════════
        # CARTELLE TELEGRAM
        # ════════════════════════════════════════

        elif text == "/lf":
            try:
                folders = await get_folders(client)
                if not folders:
                    await event.reply("📁 Nessuna cartella trovata nel tuo account.")
                    return
                lines = "\n".join(
                    f"  {i+1}. **{get_folder_title(f)}**  ({len(f.include_peers)} chat)"
                    for i, f in enumerate(folders)
                )
                await event.reply(
                    f"📁 **CARTELLE TELEGRAM**\n"
                    f"{SEPL}\n"
                    f"{lines}\n"
                    f"{SEP}\n"
                    f"`/sf NomeCartella` → aggiungi come **sorgenti**\n"
                    f"`/tf NomeCartella` → aggiungi come **destinazioni**"
                )
            except Exception as e:
                await event.reply(f"❌ Errore nel recupero cartelle:\n`{e}`")

        elif text.startswith("/sf"):
            parts = text.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            if name:
                await add_folder_to_list(client, event, name, is_source=True)
            else:
                await event.reply("ℹ️ Uso: `/sf NomeCartella`\n\n_Manda /lf per vedere le cartelle disponibili._")

        elif text.startswith("/tf"):
            parts = text.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            if name:
                await add_folder_to_list(client, event, name, is_source=False)
            else:
                await event.reply("ℹ️ Uso: `/tf NomeCartella`\n\n_Manda /lf per vedere le cartelle disponibili._")

        # ════════════════════════════════════════
        # SORGENTI PER SLAVE SPECIFICO
        # ════════════════════════════════════════

        elif text.startswith("/sa "):
            try:
                _, slave_n, link = text.split(maxsplit=2)
                slave_n = str(int(slave_n))
                entity  = await client.get_entity(link.strip())
                peer_id = get_peer_id(entity)
                name    = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
                config.setdefault("slave_sources", {}).setdefault(slave_n, [])
                if peer_id not in config["slave_sources"][slave_n]:
                    config["slave_sources"][slave_n].append(peer_id)
                    save_config(config)
                    await update_slave_config(client, config)
                    total = len(config["slave_sources"][slave_n])
                    await event.reply(
                        f"✅ **Sorgente aggiunta allo slave {slave_n}**\n"
                        f"{SEP}\n"
                        f"📌 {name}\n"
                        f"_Totale sorgenti slave {slave_n}: {total}_"
                    )
                else:
                    await event.reply(f"⚠️ **Già presente** nello slave {slave_n}:\n`{name}`")
            except Exception as e:
                await event.reply(
                    f"❌ **Errore**\n{SEP}\n`{e}`\n\n"
                    f"**Uso corretto:** `/sa 1 https://t.me/canale`"
                )

        elif text.startswith("/ssl "):
            try:
                slave_n = str(int(text.split()[1]))
                ids     = config.get("slave_sources", {}).get(slave_n, [])
                if not ids:
                    await event.reply(
                        f"📥 **Sorgenti slave {slave_n}**\n"
                        f"{SEP}\n"
                        f"Nessuna sorgente propria — usa quelle del master ({len(config['sources'])}).\n\n"
                        f"_/sa {slave_n} https://t.me/canale per aggiungerne una_"
                    )
                else:
                    names = []
                    for pid in ids:
                        try:
                            ent = await client.get_entity(pid)
                            names.append(f"@{ent.username}" if getattr(ent, "username", None) else getattr(ent, "title", str(pid)))
                        except Exception:
                            names.append(str(pid))
                    lines = "\n".join(f"  {i+1}. {n}" for i, n in enumerate(names))
                    await event.reply(
                        f"📥 **Sorgenti slave {slave_n}** ({len(names)})\n"
                        f"{SEPL}\n"
                        f"{lines}\n"
                        f"{SEP}\n"
                        f"`/sa {slave_n} link` per aggiungere · `/sra {slave_n}` per resettare"
                    )
            except Exception:
                await event.reply("ℹ️ Uso: `/ssl 1`")

        elif text.startswith("/sra "):
            try:
                slave_n = str(int(text.split()[1]))
                n_old   = len(config.get("slave_sources", {}).get(slave_n, []))
                config.setdefault("slave_sources", {}).pop(slave_n, None)
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(
                    f"🔄 **Sorgenti slave {slave_n} resettate**\n"
                    f"{SEP}\n"
                    f"Rimosse {n_old} sorgenti proprie.\n"
                    f"Lo slave {slave_n} ora usa le **sorgenti master** ({len(config['sources'])})."
                )
            except Exception:
                await event.reply("ℹ️ Uso: `/sra 1`")

        # ════════════════════════════════════════
        # ADDLIST SLAVE (t.me/addlist/...)
        # ════════════════════════════════════════

        elif text == "/al":
            s_al = config.get("slave_addlists", {})
            g_al = config.get("global_addlists", [])
            msg  = f"🔗 **ADDLIST CONFIGURATE**\n{SEPL}\n"

            # Globali
            if g_al:
                msg += f"**🌐 Globali** (tutti gli slave) — {len(g_al)}:\n"
                for i, url in enumerate(g_al):
                    msg += f"  `{i+1}.` {url}\n"
            else:
                msg += "_Nessuna addlist globale._\n"

            msg += f"\n{SEP}\n"

            # Per slave specifici
            if s_al:
                for k in sorted(s_al.keys(), key=int):
                    links = s_al[k]
                    msg  += f"**Slave {k}** — {len(links)}:\n"
                    for i, url in enumerate(links):
                        msg += f"  `{i+1}.` {url}\n"
            else:
                msg += "_Nessuna addlist per slave specifici._\n"

            msg += (
                f"\n{SEPL}\n"
                f"**Comandi:**\n"
                f"`/ala N link` — addlist per slave N\n"
                f"`/alg link` — addlist globale (tutti)\n"
                f"`/all N` — lista slave N\n"
                f"`/alr N` — rimuovi tutte slave N\n"
                f"`/alra N I` — rimuovi la n.I dello slave N\n"
                f"`/algl` — lista globali\n"
                f"`/algr` — rimuovi tutte le globali"
            )
            await event.reply(msg)

        elif text.startswith("/ala "):
            try:
                _, slave_n, link = text.split(maxsplit=2)
                slave_n = str(int(slave_n))
                link    = link.strip()
                config.setdefault("slave_addlists", {}).setdefault(slave_n, [])
                if link not in config["slave_addlists"][slave_n]:
                    config["slave_addlists"][slave_n].append(link)
                    save_config(config)
                    await update_slave_config(client, config)
                    total = len(config["slave_addlists"][slave_n])
                    await event.reply(
                        f"✅ **Addlist aggiunta per slave {slave_n}**\n"
                        f"{SEP}\n"
                        f"{link}\n"
                        f"_Totale addlist slave {slave_n}: {total}_\n\n"
                        f"Lo slave entrerà nei gruppi e li silenzerà al prossimo ciclo."
                    )
                else:
                    await event.reply(f"⚠️ Questa addlist è **già presente** per lo slave {slave_n}.")
            except Exception as e:
                await event.reply(
                    f"❌ **Errore**\n{SEP}\n`{e}`\n\n"
                    f"**Uso corretto:** `/ala 1 https://t.me/addlist/...`"
                )

        elif text.startswith("/all "):
            try:
                slave_n = str(int(text.split()[1]))
                links   = config.get("slave_addlists", {}).get(slave_n, [])
                if not links:
                    await event.reply(
                        f"🔗 **Addlist slave {slave_n}**\n"
                        f"{SEP}\n"
                        f"Nessuna addlist configurata.\n\n"
                        f"_/ala {slave_n} https://t.me/addlist/... per aggiungerne una_"
                    )
                else:
                    lines = "\n".join(f"  `{i+1}.` {url}" for i, url in enumerate(links))
                    await event.reply(
                        f"🔗 **Addlist slave {slave_n}** ({len(links)})\n"
                        f"{SEPL}\n"
                        f"{lines}\n"
                        f"{SEP}\n"
                        f"`/alra {slave_n} N` per rimuovere la n.N · `/alr {slave_n}` per rimuovere tutte"
                    )
            except Exception:
                await event.reply("ℹ️ Uso: `/all 1`")

        elif text.startswith("/alr "):
            try:
                slave_n = str(int(text.split()[1]))
                n       = len(config.get("slave_addlists", {}).get(slave_n, []))
                config.setdefault("slave_addlists", {}).pop(slave_n, None)
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(f"🗑 **{n} addlist rimosse** dallo slave {slave_n}.")
            except Exception:
                await event.reply("ℹ️ Uso: `/alr 1`")

        elif text.startswith("/alra "):
            try:
                _, slave_n, idx_str = text.split(maxsplit=2)
                slave_n = str(int(slave_n))
                idx     = int(idx_str) - 1
                links   = config.setdefault("slave_addlists", {}).get(slave_n, [])
                if 0 <= idx < len(links):
                    removed = links.pop(idx)
                    if not links:
                        config["slave_addlists"].pop(slave_n, None)
                    save_config(config)
                    await update_slave_config(client, config)
                    await event.reply(
                        f"🗑 **Addlist n.{idx+1} rimossa** dallo slave {slave_n}\n"
                        f"{SEP}\n"
                        f"{removed}"
                    )
                else:
                    await event.reply(
                        f"❌ Numero non valido.\n"
                        f"Lo slave {slave_n} ha {len(links)} addlist.\n\n"
                        f"_/all {slave_n} per vedere la lista_"
                    )
            except Exception as e:
                await event.reply(
                    f"❌ **Errore**\n{SEP}\n`{e}`\n\n"
                    f"**Uso corretto:** `/alra 1 2`  _(slave 1, addlist numero 2)_"
                )

        elif text.startswith("/alg "):
            try:
                link = text.split(maxsplit=1)[1].strip()
                config.setdefault("global_addlists", [])
                if link not in config["global_addlists"]:
                    config["global_addlists"].append(link)
                    save_config(config)
                    await update_slave_config(client, config)
                    total = len(config["global_addlists"])
                    await event.reply(
                        f"✅ **Addlist globale aggiunta** ({total} totali)\n"
                        f"{SEP}\n"
                        f"{link}\n\n"
                        f"Tutti gli slave entreranno in questa cartella e la silenzeranno."
                    )
                else:
                    await event.reply("⚠️ Questa addlist è **già presente** nelle globali.")
            except Exception as e:
                await event.reply(
                    f"❌ **Errore**\n{SEP}\n`{e}`\n\n"
                    f"**Uso corretto:** `/alg https://t.me/addlist/...`"
                )

        elif text == "/algl":
            links = config.get("global_addlists", [])
            if not links:
                await event.reply(
                    f"🌐 **Addlist globali**\n{SEP}\n"
                    f"Nessuna addlist globale configurata.\n\n"
                    f"_/alg https://t.me/addlist/... per aggiungerne una_"
                )
            else:
                lines = "\n".join(f"  `{i+1}.` {url}" for i, url in enumerate(links))
                await event.reply(
                    f"🌐 **Addlist globali** ({len(links)})\n"
                    f"{SEPL}\n"
                    f"{lines}\n"
                    f"{SEP}\n"
                    f"_/algr per rimuovere tutte_"
                )

        elif text == "/algr":
            n = len(config.get("global_addlists", []))
            config["global_addlists"] = []
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(f"🗑 **{n} addlist globali rimosse.**")

        # ════════════════════════════════════════
        # ENTITÀ SINGOLA (link diretto)
        # ════════════════════════════════════════

        elif text.startswith("/a "):
            await add_entity(client, event, text.split(maxsplit=1)[1].strip(), is_source=True)

        elif text.startswith("/d "):
            await add_entity(client, event, text.split(maxsplit=1)[1].strip(), is_source=False)

        elif "t.me/addlist/" in text.lower() or "telegram.me/addlist/" in text.lower():
            # ── Link t.me/addlist/ rilevato automaticamente ────────
            # Estraiamo solo l'URL dal messaggio
            import re
            match = re.search(r'https?://(?:t\.me|telegram\.me)/addlist/\S+', text, re.IGNORECASE)
            extracted_link = match.group(0) if match else text.strip()
            globals()["pending_addlist"] = extracted_link
            globals()["pending_link"]    = None
            await event.reply(
                f"🔗 **Addlist rilevata!**\n{SEPL}\n"
                f"`{text.strip()}`\n{SEP}\n"
                f"Come vuoi aggiungerla?\n\n"
                f"  • `globale` — tutti gli slave entrano nella cartella\n"
                f"  • `slave 1` — solo lo slave 1  _(o 2, 3, ecc.)_\n\n"
                f"_Rispondi con una delle opzioni sopra._"
            )

        elif any(x in text.lower() for x in ("t.me/", "telegram.me")):
            globals()["pending_link"] = text
            globals()["pending_addlist"] = None
            await event.reply(
                f"🔗 **Link Telegram rilevato!**\n{SEP}\n"
                f"Rispondi con:\n"
                f"  • `sorgente` — aggiunge come sorgente master\n"
                f"  • `destinazione` — aggiunge come destinazione"
            )

        elif text.lower() == "globale" and globals().get("pending_addlist"):
            # Risposta al prompt addlist → aggiunge come globale
            link = globals()["pending_addlist"]
            globals()["pending_addlist"] = None
            config.setdefault("global_addlists", [])
            if link not in config["global_addlists"]:
                config["global_addlists"].append(link)
                save_config(config)
                await update_slave_config(client, config)
                total = len(config["global_addlists"])
                await event.reply(
                    f"✅ **Addlist globale aggiunta** ({total} totali)\n"
                    f"{SEP}\n"
                    f"{link}\n\n"
                    f"Tutti gli slave entreranno nella cartella e la silenzeranno."
                )
            else:
                await event.reply("⚠️ Questa addlist è **già presente** nelle globali.")

        elif text.lower().startswith("slave ") and globals().get("pending_addlist"):
            # Risposta al prompt addlist → aggiunge per slave specifico
            try:
                slave_n = str(int(text.split()[1]))
                link    = globals()["pending_addlist"]
                globals()["pending_addlist"] = None
                config.setdefault("slave_addlists", {}).setdefault(slave_n, [])
                if link not in config["slave_addlists"][slave_n]:
                    config["slave_addlists"][slave_n].append(link)
                    save_config(config)
                    await update_slave_config(client, config)
                    total = len(config["slave_addlists"][slave_n])
                    await event.reply(
                        f"✅ **Addlist aggiunta per slave {slave_n}** ({total} totali)\n"
                        f"{SEP}\n"
                        f"{link}\n\n"
                        f"Lo slave entrerà nei gruppi e li silenzerà al prossimo ciclo."
                    )
                else:
                    await event.reply(f"⚠️ Addlist già presente per lo slave {slave_n}.")
            except Exception:
                await event.reply(
                    f"❌ Numero slave non valido.\n"
                    f"Rispondi `globale` oppure `slave 1` (o 2, 3, ecc.)."
                )

        elif text.lower() in ("sorgente", "destinazione"):
            if globals().get("pending_link"):
                await add_entity(client, event, globals()["pending_link"], text.lower() == "sorgente")
                globals()["pending_link"] = None
            else:
                await event.reply("⚠️ Nessun link in attesa. Manda prima un link Telegram.")

        # ════════════════════════════════════════
        # INTERVALLO
        # ════════════════════════════════════════

        elif text.startswith("/i "):
            try:
                mins = max(1, int(text.split()[1]))
                config["interval"] = mins
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(
                    f"⏰ **Intervallo master aggiornato**\n"
                    f"{SEP}\n"
                    f"Nuovo intervallo: **{mins} minuti**\n\n"
                    f"_Gli slave senza intervallo personalizzato useranno questo valore._"
                )
            except Exception:
                await event.reply("ℹ️ Uso: `/i 10`  _(numero di minuti)_")

        elif text.startswith("/si "):
            try:
                _, slave_n, mins_str = text.split()
                slave_n = str(int(slave_n))
                mins    = max(1, int(mins_str))
                config.setdefault("slave_intervals", {})[slave_n] = mins
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(
                    f"🕐 **Intervallo slave {slave_n} aggiornato**\n"
                    f"{SEP}\n"
                    f"Nuovo intervallo: **{mins} minuti**"
                )
            except Exception:
                await event.reply("ℹ️ Uso: `/si 1 5`  _(slave N - minuti M)_")

        elif text == "/sil":
            si = config.get("slave_intervals", {})
            if not si:
                await event.reply(
                    f"🕐 **Intervalli slave**\n{SEP}\n"
                    f"Nessun intervallo personalizzato.\n"
                    f"Tutti gli slave usano il default master: **{config['interval']} min**\n\n"
                    f"_/si N M per impostarlo_"
                )
            else:
                lines = "\n".join(f"  • Slave {k}: **{v} min**" for k, v in sorted(si.items(), key=lambda x: int(x[0])))
                await event.reply(
                    f"🕐 **Intervalli slave**\n{SEPL}\n"
                    f"{lines}\n{SEP}\n"
                    f"Default master: {config['interval']} min\n\n"
                    f"_/sir per resettare tutti al default_"
                )

        elif text == "/sir":
            n = len(config.get("slave_intervals", {}))
            config["slave_intervals"] = {}
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                f"🔄 **{n} intervalli slave resettati**\n"
                f"{SEP}\n"
                f"Tutti gli slave usano ora il default master: **{config['interval']} min**"
            )

        # ════════════════════════════════════════
        # DEBUG / REFRESH
        # ════════════════════════════════════════

        elif text == "/refresh":
            await update_slave_config(client, config)
            if os.path.exists(SLAVE_CONFIG_FILE):
                with open(SLAVE_CONFIG_FILE, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                al_slave = {k: len(v) for k, v in sc.get("slave_addlists", {}).items()}
                await event.reply(
                    f"🔄 **slave_config.json rigenerato**\n"
                    f"{SEPL}\n"
                    f"📥 Sorgenti master:   {len(sc.get('sources', []))}\n"
                    f"📤 Destinazioni:      {len(sc.get('targets', []))}\n"
                    f"▶️ Running:           {'Sì' if sc.get('running') else 'No'}\n"
                    f"⏱ Intervallo:        {sc.get('interval')} min\n"
                    f"🔀 Sorgenti slave:   {len(sc.get('slave_sources', {}))}\n"
                    f"🔗 Addlist globali:  {len(sc.get('global_addlists', []))}\n"
                    f"🔗 Addlist slave:    {al_slave or 'nessuna'}\n"
                    f"{SEP}\n"
                    f"_Tutti gli slave riceveranno la config aggiornata al prossimo poll._"
                )
            else:
                await event.reply("⚠️ slave_config.json non trovato. Manda /on per generarlo.")

        elif text == "/debug":
            if not os.path.exists(SLAVE_CONFIG_FILE):
                await update_slave_config(client, config)
            if os.path.exists(SLAVE_CONFIG_FILE):
                with open(SLAVE_CONFIG_FILE, "r", encoding="utf-8") as f:
                    content = f.read()
                await event.reply(
                    f"🔍 **slave_config.json** _(cosa vedono gli slave)_\n"
                    f"{SEP}\n"
                    f"```\n{content[:2800]}\n```"
                )
            else:
                await event.reply("❌ slave_config.json non trovato. Manda `/on` o `/refresh`.")

        # ════════════════════════════════════════
        # AIUTO
        # ════════════════════════════════════════

        elif text in ("/h", "/help"):
            await event.reply(
                f"📖 **GUIDA COMANDI MASTER**\n"
                f"{SEPL}\n"

                f"▶️ **CONTROLLO**\n"
                f"`/on` — avvia il bot (invia subito)\n"
                f"`/off` — ferma il bot\n"
                f"`/s` — stato completo\n"
                f"`/reset` — azzera sorgenti e destinazioni\n"
                f"{SEP}\n"

                f"🔗 **SORGENTI E DESTINAZIONI**\n"
                f"`/a link` — aggiungi sorgente master\n"
                f"`/d link` — aggiungi destinazione\n"
                f"`/lf` — lista cartelle Telegram\n"
                f"`/sf Nome` — sorgenti da cartella\n"
                f"`/tf Nome` — destinazioni da cartella\n"
                f"{SEP}\n"

                f"🤖 **SLAVE SPECIFICI — Sorgenti**\n"
                f"`/sa N link` — sorgente per slave N\n"
                f"`/ssl N` — lista sorgenti slave N\n"
                f"`/sra N` — resetta sorgenti slave N\n"
                f"{SEP}\n"

                f"🔗 **ADDLIST** _(t.me/addlist/...)_\n"
                f"`/alg link` — addlist globale (tutti gli slave)\n"
                f"`/ala N link` — addlist per slave N\n"
                f"`/al` — mostra tutte\n"
                f"`/algl` — lista globali\n"
                f"`/all N` — lista slave N\n"
                f"`/algr` — rimuovi tutte globali\n"
                f"`/alr N` — rimuovi tutte slave N\n"
                f"`/alra N I` — rimuovi n.I dello slave N\n"
                f"{SEP}\n"

                f"⏱ **INTERVALLI**\n"
                f"`/i M` — master a M minuti\n"
                f"`/si N M` — slave N a M minuti\n"
                f"`/sil` — lista intervalli slave\n"
                f"`/sir` — resetta tutti al default\n"
                f"{SEP}\n"

                f"🔘 **BOTTONI INLINE**\n"
                f"`/b` — mostra bottoni + istruzioni\n"
                f"`/bclear` — rimuovi tutti\n"
                f"{SEP}\n"

                f"💬 **AUTO-RISPOSTA PM SLAVE**\n"
                f"`/rt testo` — imposta  _(alias di /replytext)_\n"
                f"`/rt` — mostra testo attuale\n"
                f"`/rs` — stato  _(alias di /replyshow)_\n"
                f"`/rc` — cancella  _(alias di /replyclear)_\n"
                f"{SEP}\n"

                f"🔧 **DIAGNOSTICA**\n"
                f"`/debug` — contenuto slave_config.json\n"
                f"`/refresh` — rigenera slave_config.json\n"
                f"{SEPL}\n"
                f"💡 **SCORCIATOIE**\n"
                f"• Incolla un link `t.me/addlist/...` direttamente → il bot chiede se globale o per quale slave\n"
                f"• Incolla un link `t.me/...` normale → il bot chiede se sorgente o destinazione\n"
                f"{SEP}\n"
                f"_Manda /s per vedere lo stato attuale del bot._"
            )

    # ─── AVVIO TASK ───────────────────────────────────────────────
    spam_task = asyncio.create_task(spam_loop(client, config))
    http_task = asyncio.create_task(start_http_server())

    logger.info("🎉 Master pronto! Invia comandi in 'Messaggi Salvati'")
    await client.run_until_disconnected()

    spam_task.cancel()
    http_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
