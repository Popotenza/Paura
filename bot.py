"""
Telegram Master Userbot
========================
Controlla tutti gli slave via HTTP. Invia comandi dai tuoi Messaggi Salvati.

── VARIABILI D'AMBIENTE ───────────────────────────────────────────────────────
  API_ID          — API ID dell'account master (da my.telegram.org)
  API_HASH        — API Hash dell'account master
  SESSION_STRINAostati
  /sir            — resetta tutti gli intervalli slave al default

  Bottoni inline:
  /b              — mostra bottoni attuali + istruzioni
  /bclear         — rimuovi tutti i bottoni

  Cartelle Telegram:
  /lf             — lista cartelle
  /sf NomeCartella — aggiungi cartella come sorgenti
  /tf NomeCartella — aggiungi cartella come destinazioni

  Canale singolo:
  /a https://t.me/... — aggiungi sorgente
  /d https://t.me/... — aggiungi destinazione

  Auto-risposta PM (per gli slave):
  /replytext Ciao {first_name}! ... — imposta testo auto-risposta
  /replytext            — mostra testo attuale
  /replyshow            — mostra testo attuale
  /replyclear           — cancella il testo auto-risposta

  /h              — aiuto
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

CONFIG_FILE       = os.path.join(os.path.dirname(__file__), "config.json")
SLAVE_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "slave_config.json")

trigger_now = asyncio.Event()


# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("buttons_rows", [])
        cfg.setdefault("interval", 10)
        cfg.setdefault("slave_intervals", {})
        cfg.setdefault("slave_sources", {})
        cfg.setdefault("last_ids", {})
        cfg.setdefault("running", True)
        cfg.setdefault("rotation_indices", {})
        cfg.setdefault("auto_reply_text", "")
        return cfg
    return {
        "sources": [],
        "targets": [],
        "interval": 10,
        "slave_intervals": {},
        "slave_sources": {},
        "last_ids": {},
        "running": True,
        "buttons_rows": [],
        "rotation_indices": {},
        "auto_reply_text": "",
    }

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

async def update_slave_config(client, config):
    """Genera slave_config.json con username leggibili. Letto dagli slave via HTTP."""
    async def resolve_name(peer_id):
        try:
            ent = await client.get_entity(peer_id)
            username = getattr(ent, "username", None)
            if username:
                return f"@{username}"
            title = getattr(ent, "title", None)
            if title:
                return title
        except Exception:
            pass
        return str(peer_id)

    sources = [await resolve_name(s) for s in config.get("sources", [])]
    targets = [await resolve_name(t) for t in config.get("targets", [])]

    resolved_slave_sources = {}
    for slave_n, peer_ids in config.get("slave_sources", {}).items():
        resolved_slave_sources[slave_n] = [await resolve_name(p) for p in peer_ids]

    slave_cfg = {
        "sources": sources,
        "targets": targets,
        "buttons_rows": config.get("buttons_rows", []),
        "interval": config.get("interval", 10),
        "slave_intervals": config.get("slave_intervals", {}),
        "slave_sources": resolved_slave_sources,
        "running": config.get("running", True),
        "auto_reply_text": config.get("auto_reply_text", ""),
    }
    with open(SLAVE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(slave_cfg, f, ensure_ascii=False, indent=4)
    logger.info("🔄 slave_config.json aggiornato")


# ── Bottoni ───────────────────────────────────────────────────────────────────

COLOR_MAP = {"#g": "🟢", "#r": "🔴", "#p": "🔵"}

def _apply_color(text: str) -> str:
    for prefix, emoji in COLOR_MAP.items():
        if text.lower().startswith(prefix + " "):
            return emoji + " " + text[len(prefix):].strip()
        if text.lower().startswith(prefix):
            return emoji + text[len(prefix):]
    return text

def _make_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if raw_url.lower().startswith("share:"):
        share_text = raw_url[6:].strip()
        encoded = urllib.parse.quote(share_text, safe="")
        return f"https://t.me/share/url?text={encoded}"
    return raw_url

def parse_buttons(definition: str) -> list[list[dict]]:
    rows = []
    for line in definition.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for btn_def in line.split("&&"):
            btn_def = btn_def.strip()
            if " - " not in btn_def:
                logger.warning(f"Bottone ignorato: {btn_def!r}")
                continue
            parts = btn_def.split(" - ", 1)
            btn_text = _apply_color(parts[0].strip())
            btn_url  = _make_url(parts[1].strip())
            if btn_text and btn_url:
                row.append({"text": btn_text, "url": btn_url})
        if row:
            rows.append(row)
    return rows

def build_buttons(config) -> list | None:
    rows = config.get("buttons_rows", [])
    if not rows:
        return None
    return [[Button.url(btn["text"], btn["url"]) for btn in row] for row in rows]

def format_buttons_preview(rows: list[list[dict]]) -> str:
    if not rows:
        return "Nessun bottone configurato."
    return "\n".join("  " + " | ".join(f"[{b['text']}]" for b in row) for row in rows)


# ── Cartelle ──────────────────────────────────────────────────────────────────

def get_folder_title(f) -> str:
    title = f.title
    if isinstance(title, str):
        return title
    if hasattr(title, "text"):
        return title.text
    return str(title)

async def get_folders(client):
    result = await client(GetDialogFiltersRequest())
    return [f for f in result.filters if hasattr(f, "include_peers")]

async def resolve_folder_peers(client, folder):
    peers = []
    for peer in folder.include_peers:
        try:
            entity = await client.get_entity(peer)
            peer_id = get_peer_id(entity)
            name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
            peers.append((peer_id, name))
        except Exception as e:
            logger.warning(f"Impossibile risolvere peer {peer}: {e}")
    return peers

async def add_folder_to_list(client, event, folder_name: str, is_source: bool):
    global config
    folders = await get_folders(client)
    matched = next((f for f in folders if get_folder_title(f).lower() == folder_name.lower()), None)
    if not matched:
        folder_list = "\n".join(f"• {get_folder_title(f)}" for f in folders) or "Nessuna cartella trovata"
        await event.reply(f"❌ Cartella **{folder_name}** non trovata.\n\n**Disponibili:**\n{folder_list}")
        return
    peers = await resolve_folder_peers(client, matched)
    if not peers:
        await event.reply("⚠️ Cartella vuota o non risolvibile.")
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
    msg = f"📁 **{get_folder_title(matched)}**\n\n"
    if added:
        msg += f"✅ Aggiunti come {tipo} ({len(added)}):\n" + "\n".join(f"  • {n}" for n in added) + "\n"
    if skipped:
        msg += f"\n⚠️ Già presenti ({len(skipped)}):\n" + "\n".join(f"  • {n}" for n in skipped)
    await event.reply(msg)


# ── Invio messaggi ────────────────────────────────────────────────────────────

async def copy_to_target(client, msg, target, config, _retries=0):
    try:
        text     = msg.message or getattr(msg, "caption", "") or ""
        entities = msg.entities or []
        buttons  = build_buttons(config)
        if msg.media:
            try:
                await client.send_file(
                    target, file=msg.media, caption=text,
                    formatting_entities=entities, buttons=buttons, silent=False
                )
            except Exception as media_err:
                logger.warning(f"Media fallito su {target} ({media_err}) — invio solo testo")
                if text:
                    await client.send_message(
                        target, text,
                        formatting_entities=entities, buttons=buttons
                    )
        else:
            await client.send_message(
                target, text,
                formatting_entities=entities, buttons=buttons
            )
        logger.info(f"✅ msg {msg.id} → {target}")
    except FloodWaitError as e:
        if _retries >= 3:
            logger.error(f"FloodWait ripetuto ({_retries}x) su {target}, messaggio saltato.")
            return
        logger.warning(f"FloodWait {e.seconds}s (tentativo {_retries + 1}/3)")
        await asyncio.sleep(e.seconds + 1)
        await copy_to_target(client, msg, target, config, _retries + 1)
    except Exception as e:
        logger.error(f"Errore → {target}: {e}")

async def send_to_all(client, msg, config):
    if not config["targets"]:
        return
    await asyncio.gather(*[copy_to_target(client, msg, t, config) for t in config["targets"]])


# ── Spam loop ─────────────────────────────────────────────────────────────────

async def spam_loop(client, config):
    while True:
        if not config["running"]:
            await asyncio.sleep(5)
            continue
        try:
            await asyncio.wait_for(trigger_now.wait(), timeout=config["interval"] * 60)
            trigger_now.clear()
        except asyncio.TimeoutError:
            pass
        if not config["running"]:
            continue
        updated = False
        for source in config["sources"][:]:
            try:
                all_msgs = await client.get_messages(source, limit=200)
                valid = sorted(
                    [m for m in all_msgs if m.message or m.media],
                    key=lambda m: m.id
                )
                if not valid:
                    logger.info(f"Nessun post valido in {source}")
                    continue
                key = str(source)
                idx = config.setdefault("rotation_indices", {}).get(key, 0)
                idx = idx % len(valid)
                msg = valid[idx]
                logger.info(f"📤 Post {idx+1}/{len(valid)} (id={msg.id}) da {source}")
                await send_to_all(client, msg, config)
                config["rotation_indices"][key] = (idx + 1) % len(valid)
                updated = True
            except Exception as e:
                logger.error(f"Errore sorgente {source}: {e}")
        if updated:
            save_config(config)


# ── Aggiungi entità ───────────────────────────────────────────────────────────

async def add_entity(client, event, link: str, is_source: bool):
    global config
    try:
        entity = await client.get_entity("me" if link.lower() in ["me", "saved"] else link.strip())
        peer_id = get_peer_id(entity)
        key = "sources" if is_source else "targets"
        if peer_id not in config[key]:
            config[key].append(peer_id)
            save_config(config)
            await update_slave_config(client, config)
            name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
            await event.reply(f"✅ Aggiunto come **{'sorgente' if is_source else 'destinazione'}**: {name}")
        else:
            await event.reply("⚠️ Già presente!")
    except Exception as e:
        await event.reply(f"❌ Impossibile aggiungere:\n{str(e)}")


# ── HTTP server ───────────────────────────────────────────────────────────────

async def start_http_server():
    async def handle_slave_config(request):
        if not os.path.exists(SLAVE_CONFIG_FILE):
            return web.Response(
                status=503,
                text=json.dumps({"error": "Config non ancora generata dal bot"}),
                content_type="application/json"
            )
        with open(SLAVE_CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="application/json")

    async def handle_health(request):
        return web.Response(text=json.dumps({"status": "ok"}), content_type="application/json")

    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/api/slave-config", handle_slave_config)
    app.router.add_get("/healthz",          handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 HTTP server avviato su porta {port}")
    while True:
        await asyncio.sleep(3600)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
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
        print("✅ Salva questa SESSION_STRING nelle variabili d'ambiente:")
        print(client.session.save())
        print("=" * 60 + "\n")

    config = load_config()
    logger.info(f"Avviato | {len(config['sources'])} sorgenti | {len(config['targets'])} destinazioni")

    if config["running"]:
        trigger_now.set()

    globals()["pending_link"] = None

    @client.on(events.NewMessage(chats="me"))
    async def command_handler(event):
        global config
        text = (event.message.text or "").strip()

        if not text:
            return

        # ── Comandi ───────────────────────────────────────────────────────────

        if text in ["/on", "/start"] or text.startswith("/on ") or text.startswith("/start "):
            config["running"] = True
            save_config(config)
            await update_slave_config(client, config)
            trigger_now.set()
            await event.reply("✅ Avviato — controllo immediato in corso...")

        elif text in ["/off", "/stop"] or text.startswith("/off ") or text.startswith("/stop "):
            config["running"] = False
            save_config(config)
            await update_slave_config(client, config)
            await event.reply("⛔ Fermato.")

        elif text == "/s":
            stato = "🟢 Attivo" if config["running"] else "🔴 Fermo"
            n_btn = sum(len(r) for r in config.get("buttons_rows", []))
            reply_text = config.get("auto_reply_text", "")
            slave_intervals = config.get("slave_intervals", {})
            si_lines = ""
            if slave_intervals:
                si_lines = "\n" + "\n".join(
                    f"  • Slave {k}: {v} min" for k, v in sorted(slave_intervals.items(), key=lambda x: int(x[0]))
                )
            out = (
                f"📊 **STATO** — {stato}\n\n"
                f"⏰ Intervallo master: {config['interval']} min\n"
                f"🕐 Intervalli slave:{si_lines or ' (default master)'}\n"
                f"📥 Sorgenti: {len(config['sources'])}\n"
                f"📤 Destinazioni: {len(config['targets'])}\n"
                f"🔘 Bottoni: {n_btn}\n"
                f"💬 Auto-risposta: {'✅ attiva' if reply_text else '❌ non impostata'}"
            )
            await event.reply(out)

        elif text == "/reset":
            config["sources"]          = []
            config["targets"]          = []
            config["last_ids"]         = {}
            config["rotation_indices"] = {}
            save_config(config)
            await update_slave_config(client, config)
            await event.reply("🔄 Reset: sorgenti, destinazioni e cronologia azzerati.")

        # ── Bottoni ───────────────────────────────────────────────────────────

        elif text == "/b":
            rows    = config.get("buttons_rows", [])
            preview = format_buttons_preview(rows)
            await event.reply(
                f"**🔘 Bottoni attuali:**\n{preview}\n\n"
                "**Per impostare i bottoni** manda:\n"
                "```\n/b\n🔥 Canale - https://t.me/tuocanale\n"
                "#g Contatto - https://t.me/user && #r Limitati - https://t.me/gruppo\n"
                "Condividi - share:Dai un'occhiata!\n```\n\n"
                "• `&&` → stessa riga\n• `#g` 🟢  `#r` 🔴  `#p` 🔵\n• `/bclear` → rimuovi tutti"
            )

        elif text.startswith("/b\n") or (text.startswith("/b ") and len(text) > 3):
            definition = text[2:].strip()
            if not definition:
                await event.reply("Definizione vuota. Scrivi `/b` per le istruzioni.")
                return
            try:
                rows = parse_buttons(definition)
                if not rows:
                    await event.reply("❌ Nessun bottone valido trovato.\n\nFormato: `testo - https://url`")
                    return
                config["buttons_rows"] = rows
                save_config(config)
                await update_slave_config(client, config)
                preview = format_buttons_preview(rows)
                total   = sum(len(r) for r in rows)
                await event.reply(f"✅ **{total} bottoni impostati** su {len(rows)} righe:\n\n{preview}")
            except Exception as e:
                await event.reply(f"❌ Errore nel parsing:\n{str(e)}")

        elif text == "/bclear":
            config["buttons_rows"] = []
            save_config(config)
            await update_slave_config(client, config)
            await event.reply("🗑 Tutti i bottoni rimossi.")

        # ── Auto-risposta PM ──────────────────────────────────────────────────

        elif text.startswith("/replytext\n") or (text.startswith("/replytext ") and len(text) > 11):
            reply_text = text[len("/replytext"):].strip()
            config["auto_reply_text"] = reply_text
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(
                f"✅ **Testo auto-risposta impostato:**\n\n{reply_text}\n\n"
                "Segnaposto disponibili: `{first_name}` `{last_name}` `{full_name}` `{username}`"
            )

        elif text == "/replytext":
            current = config.get("auto_reply_text", "")
            if current:
                await event.reply(f"📝 **Testo auto-risposta attuale:**\n\n{current}")
            else:
                await event.reply(
                    "Nessun testo impostato.\n\n"
                    "Usa: `/replytext Ciao {first_name}! ...`\n"
                    "Segnaposto: `{first_name}` `{last_name}` `{full_name}` `{username}`"
                )

        elif text == "/replyshow":
            reply_text = config.get("auto_reply_text", "")
            out = (
                "🤖 **Auto-risposta PM slave**\n\n"
                f"📝 Testo: {reply_text or '*(non impostato)*'}\n\n"
                "Per modificare:\n"
                "• `/replytext <testo>` — imposta testo\n"
                "• `/replyclear` — cancella testo"
            )
            await event.reply(out)

        elif text == "/replyclear":
            config["auto_reply_text"] = ""
            save_config(config)
            await update_slave_config(client, config)
            await event.reply("🗑 Testo auto-risposta rimosso.")

        # ── Cartelle ──────────────────────────────────────────────────────────

        elif text == "/lf":
            try:
                folders = await get_folders(client)
                if not folders:
                    await event.reply("Nessuna cartella trovata.")
                    return
                out = "📁 **Cartelle:**\n\n"
                for f in folders:
                    out += f"• **{get_folder_title(f)}** ({len(f.include_peers)} chat)\n"
                out += "\n`/sf NomeCartella` → sorgenti\n`/tf NomeCartella` → destinazioni"
                await event.reply(out)
            except Exception as e:
                await event.reply(f"❌ {str(e)}")

        elif text.startswith("/sf"):
            parts = text.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            await (add_folder_to_list(client, event, name, True) if name
                   else event.reply("Uso: `/sf NomeCartella`"))

        elif text.startswith("/tf"):
            parts = text.split(maxsplit=1)
            name  = parts[1].strip() if len(parts) > 1 else ""
            await (add_folder_to_list(client, event, name, False) if name
                   else event.reply("Uso: `/tf NomeCartella`"))

        # ── Sorgenti per slave ────────────────────────────────────────────────

        elif text.startswith("/sa "):
            try:
                parts = text.split(maxsplit=2)
                slave_n = str(int(parts[1]))
                link    = parts[2].strip()
                entity  = await client.get_entity(link)
                peer_id = get_peer_id(entity)
                name    = getattr(entity, "title", None) or getattr(entity, "username", None) or str(peer_id)
                config.setdefault("slave_sources", {}).setdefault(slave_n, [])
                if peer_id not in config["slave_sources"][slave_n]:
                    config["slave_sources"][slave_n].append(peer_id)
                    save_config(config)
                    await update_slave_config(client, config)
                    await event.reply(f"✅ Sorgente slave **{slave_n}** aggiunta: {name}")
                else:
                    await event.reply("⚠️ Già presente!")
            except Exception as e:
                await event.reply(f"❌ Errore: {e}\n\nUso: `/sa 1 https://t.me/canale`")

        elif text.startswith("/ssl "):
            try:
                slave_n = str(int(text.split()[1]))
                ids = config.get("slave_sources", {}).get(slave_n, [])
                if not ids:
                    await event.reply(f"Slave **{slave_n}** non ha sorgenti proprie — usa quelle del master ({len(config['sources'])}).")
                else:
                    names = []
                    for pid in ids:
                        try:
                            ent = await client.get_entity(pid)
                            names.append(getattr(ent, "username", None) and f"@{ent.username}" or getattr(ent, "title", str(pid)))
                        except Exception:
                            names.append(str(pid))
                    out = "\n".join(f"  • {n}" for n in names)
                    await event.reply(f"📥 **Sorgenti slave {slave_n}:**\n{out}\n\n`/sra {slave_n}` per resettare")
            except Exception:
                await event.reply("Uso: `/ssl 1`")

        elif text.startswith("/sra "):
            try:
                slave_n = str(int(text.split()[1]))
                config.setdefault("slave_sources", {}).pop(slave_n, None)
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(f"🔄 Slave **{slave_n}** ora usa le sorgenti del master.")
            except Exception:
                await event.reply("Uso: `/sra 1`")

        # ── Entità singola ────────────────────────────────────────────────────

        elif text.startswith("/a "):
            await add_entity(client, event, text.split(maxsplit=1)[1].strip(), True)

        elif text.startswith("/d "):
            await add_entity(client, event, text.split(maxsplit=1)[1].strip(), False)

        elif any(x in text.lower() for x in ["t.me/", "telegram.me"]):
            globals()["pending_link"] = text
            await event.reply("🔗 Link rilevato! Rispondi: `sorgente` o `destinazione`")

        elif text.lower() in ["sorgente", "destinazione"]:
            if globals().get("pending_link"):
                await add_entity(client, event, globals()["pending_link"], text.lower() == "sorgente")
                globals()["pending_link"] = None
            else:
                await event.reply("Nessun link in attesa.")

        # ── Intervallo ────────────────────────────────────────────────────────

        elif text.startswith("/i "):
            try:
                mins = max(1, int(text.split()[1]))
                config["interval"] = mins
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(f"⏰ Intervallo master impostato a **{mins} minuti**.")
            except Exception:
                await event.reply("Uso: `/i 10`")

        elif text.startswith("/si "):
            try:
                parts = text.split()
                slave_n = str(int(parts[1]))
                mins    = max(1, int(parts[2]))
                config.setdefault("slave_intervals", {})[slave_n] = mins
                save_config(config)
                await update_slave_config(client, config)
                await event.reply(f"🕐 Slave **{slave_n}** → intervallo impostato a **{mins} minuti**.")
            except Exception:
                await event.reply("Uso: `/si 1 5`  (slave numero - minuti)")

        elif text == "/sil":
            si = config.get("slave_intervals", {})
            if not si:
                await event.reply(f"Nessun intervallo slave personalizzato.\nTutti usano il default master: **{config['interval']} min**")
            else:
                lines = "\n".join(f"  • Slave {k}: {v} min" for k, v in sorted(si.items(), key=lambda x: int(x[0])))
                await event.reply(f"🕐 **Intervalli slave:**\n{lines}\n\n_(default master: {config['interval']} min)_")

        elif text == "/sir":
            config["slave_intervals"] = {}
            save_config(config)
            await update_slave_config(client, config)
            await event.reply(f"🔄 Intervalli slave resettati — tutti usano il default master: **{config['interval']} min**")

        # ── Aiuto ─────────────────────────────────────────────────────────────

        elif text in ["/h", "/help"]:
            await event.reply(
                "📋 **COMANDI MASTER**\n\n"
                "`/on` — avvia (parte subito)\n"
                "`/off` — ferma\n"
                "`/s` — stato e liste\n"
                "`/reset` — azzera sorgenti e destinazioni\n\n"
                "**Bottoni inline:**\n"
                "`/b` — mostra bottoni + istruzioni\n"
                "`/bclear` — rimuovi tutti i bottoni\n\n"
                "**Cartelle Telegram:**\n"
                "`/lf` — lista cartelle\n"
                "`/sf NomeCartella` — aggiungi cartella come sorgenti\n"
                "`/tf NomeCartella` — aggiungi cartella come destinazioni\n\n"
                "**Canale singolo:**\n"
                "`/a https://t.me/...` — aggiungi sorgente\n"
                "`/d https://t.me/...` — aggiungi destinazione\n\n"
                "**Auto-risposta PM slave:**\n"
                "`/replytext Ciao {first_name}!` — imposta testo\n"
                "`/replytext` — mostra testo attuale\n"
                "`/replyshow` — mostra stato auto-risposta\n"
                "`/replyclear` — cancella testo\n\n"
                "**Impostazioni:**\n"
                "`/i 10` — intervallo master in minuti\n"
                "`/si 1 5` — intervallo slave 1 a 5 min\n"
                "`/sil` — lista intervalli slave\n"
                "`/sir` — resetta intervalli slave al default\n\n"
                "**Sorgenti per slave:**\n"
                "`/sa 1 https://t.me/canale` — aggiungi sorgente allo slave 1\n"
                "`/ssl 1` — mostra sorgenti slave 1\n"
                "`/sra 1` — resetta sorgenti slave 1 (torna al master)"
            )

    spam_task = asyncio.create_task(spam_loop(client, config))
    http_task = asyncio.create_task(start_http_server())
    logger.info("🎉 Master pronto! Invia comandi in 'Messaggi Salvati'")
    await client.run_until_disconnected()
    spam_task.cancel()
    http_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
