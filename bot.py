#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
import logging
import tempfile
import atexit
from dotenv import load_dotenv
from datetime import datetime
from threading import Lock
from telebot import TeleBot, types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot.apihelper import ApiException
from collections import defaultdict

# ————— CONFIGURAZIONE —————
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN") or "USALO_NELLA_TUA_ENV"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # ID admin
REGISTRATION_PASSWORD = os.getenv("REGISTRATION_PASSWORD", "fantamattopwd")  # Password per registrazione
bot = TeleBot(BOT_TOKEN)  # Rimosso parse_mode globale

# Configurazione logging avanzata
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ————— DATABASE SQLite inizializzazione —————
DB = sqlite3.connect("bot_matti.db", check_same_thread=False)
DB.row_factory = sqlite3.Row
CUR = DB.cursor()
db_lock = Lock()  # Lock per operazioni concorrenti

def init_db():
    with db_lock:
        CUR.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                registered INTEGER NOT NULL DEFAULT 0 CHECK (registered IN (0,1)),
                total_points INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        CUR.execute("""
            CREATE TABLE IF NOT EXISTS matti (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                points INTEGER NOT NULL
            );
        """)
        CUR.execute("""
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_chat_id INTEGER NOT NULL,
                matto_id INTEGER NOT NULL,
                points_awarded INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (user_chat_id) REFERENCES users(chat_id) ON DELETE CASCADE,
                FOREIGN KEY (matto_id) REFERENCES matti(id) ON DELETE CASCADE
            );
        """)
        DB.commit()

# ————— HELPERS DB —————
def register_user(chat_id, username, first_name):
    with db_lock:
        CUR.execute(
            "INSERT OR IGNORE INTO users(chat_id, username, first_name) VALUES(?, ?, ?);",
            (chat_id, username, first_name)
        )
        DB.commit()

def set_registered(chat_id, is_reg=True):
    with db_lock:
        CUR.execute(
            "UPDATE users SET registered = ? WHERE chat_id = ?;",
            (1 if is_reg else 0, chat_id)
        )
        DB.commit()

def unregister_user(chat_id):
    with db_lock:
        CUR.execute("UPDATE users SET registered = 0 WHERE chat_id = ?;", (chat_id,))
        DB.commit()

def get_registered_users():
    with db_lock:
        return CUR.execute(
            "SELECT chat_id, username, first_name FROM users WHERE registered = 1 ORDER BY username;"
        ).fetchall()

def get_registered_chat_ids():
    with db_lock:
        return [r["chat_id"] for r in CUR.execute(
            "SELECT chat_id FROM users WHERE registered = 1"
        ).fetchall()]

def add_sighting(chat_id, matto_id, points, file_id):
    now = datetime.utcnow().isoformat()
    with db_lock:
        CUR.execute(
            "INSERT INTO sightings(user_chat_id, matto_id, points_awarded, file_id, timestamp) VALUES(?, ?, ?, ?, ?);",
            (chat_id, matto_id, points, file_id, now)
        )
        CUR.execute(
            "UPDATE users SET total_points = total_points + ? WHERE chat_id = ?;",
            (points, chat_id)
        )
        DB.commit()

def get_leaderboard(limit=None):
    with db_lock:
        query = "SELECT chat_id, username, first_name, total_points FROM users WHERE registered = 1 ORDER BY total_points DESC"
        if limit:
            query += f" LIMIT {limit}"
        return CUR.execute(query).fetchall()

def get_user_rank_and_points(chat_id):
    with db_lock:
        res = CUR.execute(
            """
            SELECT chat_id, total_points,
                   (SELECT COUNT(*) + 1 FROM users u2
                    WHERE u2.total_points > u1.total_points AND u2.registered = 1
                   ) AS rank
            FROM users u1 WHERE chat_id = ? AND registered = 1;
            """, (chat_id,)
        ).fetchone()
        return res

def load_matti_from_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    
    parsed = []
    seen_names = set()  # Per evitare duplicati esatti
    
    for ln in lines:
        if ',' not in ln:
            logger.warning(f"Riga malformata, salto: {ln}")
            continue
        
        try:
            nome = ln.split(',', 1)[0].strip()
            pts = int(ln.split(',', 1)[1].strip())
            
            # Controlla se il nome esatto è già stato processato
            if nome in seen_names:
                logger.warning(f"Nome duplicato esatto, salto: {nome}")
                continue
                
            parsed.append((nome, pts))
            seen_names.add(nome)
            
        except ValueError:
            logger.warning(f"Riga malformata, salto: {ln}")
    
    with db_lock:
        # Disabilita FK temporaneamente
        CUR.execute("PRAGMA foreign_keys = OFF;")
        
        # Pulisci tabelle correlate
        CUR.execute("DELETE FROM sightings;")
        CUR.execute("DELETE FROM matti;")
        CUR.execute("UPDATE users SET total_points = 0;")
        
        # Riabilita FK e inserisci nuovi dati
        CUR.execute("PRAGMA foreign_keys = ON;")
        CUR.executemany(
            "INSERT INTO matti (name, points) VALUES (?, ?);", 
            parsed
        )
        DB.commit()
    
    return len(parsed)

def list_matti():
    with db_lock:
        return CUR.execute(
            "SELECT id, name, points FROM matti ORDER BY points DESC, name;"
        ).fetchall()

def get_matto_gallery(matto_id):
    with db_lock:
        return CUR.execute(
            "SELECT s.id, s.file_id, s.timestamp, u.username, u.first_name "
            "FROM sightings s "
            "JOIN users u ON s.user_chat_id = u.chat_id "
            "WHERE matto_id = ? ORDER BY s.timestamp DESC;",
            (matto_id,)
        ).fetchall()

def get_user_gallery(chat_id):
    with db_lock:
        # Ottieni tutte le segnalazioni dell'utente
        sightings = CUR.execute(
            "SELECT s.id, m.name, s.points_awarded, s.file_id, s.timestamp "
            "FROM sightings s JOIN matti m ON s.matto_id = m.id "
            "WHERE s.user_chat_id = ? ORDER BY s.timestamp DESC;",
            (chat_id,)
        ).fetchall()
        
        # Raggruppa per matto
        matto_stats = defaultdict(lambda: {"count": 0, "points": 0, "photos": []})
        for s in sightings:
            name = s["name"]
            matto_stats[name]["count"] += 1
            matto_stats[name]["points"] += s["points_awarded"]
            matto_stats[name]["photos"].append({
                "file_id": s["file_id"],
                "sighting_id": s["id"]
            })
        
        return matto_stats

def delete_sighting(sighting_id):
    with db_lock:
        # Ottieni i dettagli della segnalazione
        sighting = CUR.execute(
            "SELECT user_chat_id, points_awarded FROM sightings WHERE id = ?;",
            (sighting_id,)
        ).fetchone()
        
        if not sighting:
            return False
        
        # Elimina la segnalazione
        CUR.execute("DELETE FROM sightings WHERE id = ?;", (sighting_id,))
        
        # Aggiorna i punti dell'utente
        CUR.execute(
            "UPDATE users SET total_points = total_points - ? WHERE chat_id = ?;",
            (sighting["points_awarded"], sighting["user_chat_id"])
        )
        
        DB.commit()
        return True

# ————— STATI IN MEMORIA —————
pending_matto = {}  # chat_id → {'id':..., 'name':..., 'points':...}
pending_password = {}  # chat_id: True (in attesa di password)
admin_upload_pending = False
pending_gallery_user = {}  # chat_id → selected_user_chat_id
pending_gallery_matto = {}  # chat_id → matto_id
pending_manage_user = {}  # chat_id → selected_user_chat_id

# Pulizia stati alla chiusura
def cleanup_states():
    pending_matto.clear()
    pending_password.clear()
    pending_gallery_user.clear()
    pending_gallery_matto.clear()
    pending_manage_user.clear()
atexit.register(cleanup_states)

# ————— HANDLER COMANDI —————
@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    chat_id = msg.chat.id
    register_user(
        chat_id, 
        msg.from_user.username or "", 
        msg.from_user.first_name or ""
    )
    
    with db_lock:
        row = CUR.execute(
            "SELECT registered FROM users WHERE chat_id = ?;", 
            (chat_id,)
        ).fetchone()
    
    # Se non registrato o in attesa di password
    if not row or row['registered'] != 1:
        pending_password[chat_id] = True
        bot.send_message(
            chat_id, 
            "🔒 Per registrarti, inserisci la password:",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(
            chat_id, 
            "✅ Sei già registrato! Usa /report per segnalare un matto.",
            parse_mode="Markdown"
        )

@bot.message_handler(func=lambda message: message.chat.id in pending_password)
def handle_password(msg: types.Message):
    chat_id = msg.chat.id
    password = msg.text.strip()
    
    if password == REGISTRATION_PASSWORD:
        del pending_password[chat_id]
        set_registered(chat_id, True)
        bot.send_message(
            chat_id, 
            "✅ Password corretta! Sei registrato. Usa /report per segnalare un matto.",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(
            chat_id, 
            "❌ Password errata. Riprova o contatta l'amministratore.",
            parse_mode="Markdown"
        )

@bot.message_handler(commands=["me"])
def cmd_me(msg: types.Message):
    data = get_user_rank_and_points(msg.chat.id)
    if not data:
        bot.send_message(msg.chat.id, "🤔 Non sei registrato. Usa /start.")
        return
    
    bot.send_message(
        msg.chat.id,
        f"Sei *#{data['rank']}* in classifica con *{data['total_points']} punti*.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["comandi"])
def cmd_comandi(msg: types.Message):
    help_text = """
📜 *Lista Comandi Disponibili*

1. `/start` - Registrati al bot (richiede password)
2. `/me` - Mostra la tua posizione in classifica e punti
3. `/classifica` - Classifica completa di tutti gli sfidanti
4. `/listmatti` - Lista di tutti i matti con relativi punti
5. `/report` - Segnala un nuovo avvistamento matto
6. `/galleria_utente` - Visualizza le segnalazioni di un utente
7. `/galleria_matto` - Visualizza tutte le segnalazioni di un matto

"""

    bot.send_message(
        msg.chat.id, 
        help_text,
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["classifica"])
def cmd_full_leaderboard(msg: types.Message):
    all_users = get_leaderboard()
    if not all_users:
        bot.send_message(msg.chat.id, "🏆 La classifica è vuota!")
        return
    
    text = "🏆 *Classifica Completa*\n"
    for i, row in enumerate(all_users):
        usr = f"@{row['username']}" if row['username'] else row['first_name'] or f"Utente {i+1}"
        text += f"{i+1}. {usr} – *{row['total_points']} punti*\n"
    
    # Se il messaggio è troppo lungo, invialo come file
    if len(text) > 4000:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        
        with open(tmp_path, "rb") as f:
            bot.send_document(msg.chat.id, f, caption="Classifica completa")
        
        os.unlink(tmp_path)
    else:
        bot.send_message(msg.chat.id, text, parse_mode="Markdown")


@bot.message_handler(commands=["listmatti"])
def cmd_listmatti(msg: types.Message):
    items = list_matti()
    if not items:
        bot.send_message(
            msg.chat.id, 
            "📂 Lista matti vuota\\. L'admin può usare `/upload_matti` per caricarla\\.",
            parse_mode="MarkdownV2"
        )
        return
    
    text = "*Lista matti disponibili:*"
    for itm in items:
        text += f"\n• {itm['name']} – *{itm['points']} punti*"
    
    bot.send_message(msg.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=["galleria_utente"])
def cmd_galleria(msg: types.Message):
    users = get_registered_users()
    if not users:
        bot.send_message(msg.chat.id, "👥 Nessun utente registrato.")
        return
    
    markup = InlineKeyboardMarkup(row_width=1)
    for user in users:
        username = user['username'] or user['first_name'] or f"ID {user['chat_id']}"
        markup.add(InlineKeyboardButton(
            text=username,
            callback_data=f"select_user|{user['chat_id']}"
        ))
    
    bot.send_message(
        msg.chat.id, 
        "👤 Scegli un utente per vedere la sua galleria:", 
        reply_markup=markup
    )

@bot.message_handler(commands=["galleria_matto"])
def cmd_gallery_matto(msg: types.Message):
    items = list_matti()
    if not items:
        bot.send_message(
            msg.chat.id, 
            "📂 Nessun matto definito\\.",
            parse_mode="MarkdownV2"
        )
        return
    
    markup = InlineKeyboardMarkup(row_width=2)
    for itm in items:
        markup.add(InlineKeyboardButton(
            text=f"{itm['name']} ({itm['points']} punti)",
            callback_data=f"select_matto|{itm['id']}"
        ))
    
    bot.send_message(
        msg.chat.id, 
        "🏞️ Scegli un matto per vedere la sua galleria:", 
        reply_markup=markup
    )

@bot.message_handler(commands=["admin"])
def cmd_manage_sightings(msg: types.Message):
    if msg.chat.id != ADMIN_CHAT_ID:
        bot.send_message(msg.chat.id, "❌ Comando riservato all'admin!")
        return
    
    users = get_registered_users()
    if not users:
        bot.send_message(msg.chat.id, "👥 Nessun utente registrato.")
        return
    
    markup = InlineKeyboardMarkup(row_width=1)
    for user in users:
        username = user['username'] or user['first_name'] or f"ID {user['chat_id']}"
        markup.add(InlineKeyboardButton(
            text=username,
            callback_data=f"manage_user|{user['chat_id']}"
        ))
    
    bot.send_message(
        msg.chat.id, 
        "👤 Scegli un utente per gestire le sue segnalazioni:", 
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_user|"))
def callback_select_user(call: types.CallbackQuery):
    chat_id = call.from_user.id
    parts = call.data.split("|", 1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "ID non valido!", show_alert=True)
        return
    
    user_chat_id = int(parts[1])
    pending_gallery_user[chat_id] = user_chat_id
    
    # Crea tastiera per scegliere la modalità di visualizzazione
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Solo testo", callback_data="gallery_mode|text"),
        InlineKeyboardButton("Con foto", callback_data="gallery_mode|photos")
    )
    
    bot.send_message(
        chat_id,
        "📸 Come vuoi visualizzare la galleria di questo utente?",
        reply_markup=markup
    )
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_matto|"))
def callback_select_matto(call: types.CallbackQuery):
    chat_id = call.from_user.id
    parts = call.data.split("|", 1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "ID non valido!", show_alert=True)
        return
    
    matto_id = int(parts[1])
    pending_gallery_matto[chat_id] = matto_id
    
    # Crea tastiera per scegliere la modalità di visualizzazione
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Solo testo", callback_data="matto_mode|text"),
        InlineKeyboardButton("Con foto", callback_data="matto_mode|photos")
    )
    
    bot.send_message(
        chat_id,
        "📸 Come vuoi visualizzare la galleria di questo matto?",
        reply_markup=markup
    )
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage_user|"))
def callback_manage_user(call: types.CallbackQuery):
    chat_id = call.from_user.id
    parts = call.data.split("|", 1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "ID non valido!", show_alert=True)
        return
    
    user_chat_id = int(parts[1])
    pending_manage_user[chat_id] = user_chat_id
    
    matto_stats = get_user_gallery(user_chat_id)
    
    if not matto_stats:
        bot.send_message(chat_id, "📭 Questo utente non ha ancora segnalato nessun matto!")
        return
    
    # Ottieni i dettagli dell'utente
    with db_lock:
        user = CUR.execute(
            "SELECT username, first_name FROM users WHERE chat_id = ?;", 
            (user_chat_id,)
        ).fetchone()
    
    username = user['username'] or user['first_name'] or f"ID {user_chat_id}"
    text = f"👤 *Galleria di {username}*\n\n"
    
    for matto, stats in matto_stats.items():
        text += f"• *{matto}*: {stats['count']} segnalazioni, {stats['points']} punti\n"
    
    bot.send_message(chat_id, text, parse_mode="Markdown")
    
    # Per ogni matto, mostra le segnalazioni con pulsante elimina
    for matto, stats in matto_stats.items():
        text = f"🖼️ *{matto}* - Segnalazioni:"
        bot.send_message(chat_id, text, parse_mode="Markdown")
        
        for photo in stats["photos"]:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(
                text="❌ Elimina segnalazione",
                callback_data=f"delete_sighting|{photo['sighting_id']}"
            ))
            
            try:
                bot.send_photo(
                    chat_id, 
                    photo=photo["file_id"],
                    reply_markup=markup
                )
            except Exception as e:
                logger.error(f"Errore invio foto: {str(e)}")
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_sighting|"))
def callback_delete_sighting(call: types.CallbackQuery):
    chat_id = call.from_user.id
    parts = call.data.split("|", 1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "ID non valido!", show_alert=True)
        return
    
    sighting_id = int(parts[1])
    
    if chat_id != ADMIN_CHAT_ID:
        bot.answer_callback_query(call.id, "❌ Solo l'admin può eliminare segnalazioni!", show_alert=True)
        return
    
    if delete_sighting(sighting_id):
        bot.answer_callback_query(call.id, "✅ Segnalazione eliminata con successo!", show_alert=True)
        bot.delete_message(chat_id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ Errore durante l'eliminazione!", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("gallery_mode|"))
def callback_gallery_mode(call: types.CallbackQuery):
    chat_id = call.from_user.id
    mode = call.data.split("|")[1]
    
    if chat_id not in pending_gallery_user:
        bot.answer_callback_query(call.id, "❌ Sessione scaduta, riprova.")
        return
    
    user_chat_id = pending_gallery_user[chat_id]
    matto_stats = get_user_gallery(user_chat_id)
    
    if not matto_stats:
        bot.send_message(chat_id, "📭 Questo utente non ha segnalato nessun matto!")
        bot.answer_callback_query(call.id)
        return
    
    # Ottieni i dettagli dell'utente
    with db_lock:
        user = CUR.execute(
            "SELECT username, first_name FROM users WHERE chat_id = ?;", 
            (user_chat_id,)
        ).fetchone()
    
    username = user['username'] or user['first_name'] or f"ID {user_chat_id}"
    
    if mode == "text":
        # Visualizzazione testuale
        text = f"📋 *Galleria di {username}:*\n"
        for matto, stats in matto_stats.items():
            text += f"\n- *{matto}*: {stats['count']} volte, {stats['points']} punti"
        
        bot.send_message(chat_id, text, parse_mode="Markdown")
    
    elif mode == "photos":
        # Visualizzazione con foto
        bot.send_message(chat_id, f"📸 *Galleria di {username}:*")
        
        for matto, stats in matto_stats.items():
            text = f"*{matto}*: {stats['count']} segnalazioni, {stats['points']} punti"
            bot.send_message(chat_id, text, parse_mode="Markdown")
            
            for idx, photo in enumerate(stats["photos"], 1):
                try:
                    bot.send_photo(
                        chat_id, 
                        photo=photo["file_id"],
                        caption=f"Segnalazione {idx}/{stats['count']}"
                    )
                except Exception as e:
                    logger.error(f"Errore invio foto: {str(e)}")
    
    del pending_gallery_user[chat_id]
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("matto_mode|"))
def callback_matto_mode(call: types.CallbackQuery):
    chat_id = call.from_user.id
    mode = call.data.split("|")[1]
    
    if chat_id not in pending_gallery_matto:
        bot.answer_callback_query(call.id, "❌ Sessione scaduta, riprova.")
        return
    
    matto_id = pending_gallery_matto[chat_id]
    
    with db_lock:
        row = CUR.execute(
            "SELECT name FROM matti WHERE id = ?;", 
            (matto_id,)
        ).fetchone()
    
    if not row:
        bot.send_message(chat_id, "Matto non trovato!")
        bot.answer_callback_query(call.id)
        return
    
    name = row["name"]
    gallery = get_matto_gallery(matto_id)
    
    if not gallery:
        bot.send_message(chat_id, f"🖼️ Nessuna foto disponibile per *{name}*.")
        bot.answer_callback_query(call.id)
        return
    
    if mode == "text":
        # Visualizzazione testuale
        text = f"📋 *Segnalazioni per {name}:*\n\n"
        for sighting in gallery:
            username = sighting['username'] or sighting['first_name'] or "Utente sconosciuto"
            text += f"• {username}: {sighting['timestamp']}\n"
        
        bot.send_message(chat_id, text, parse_mode="Markdown")
    
    elif mode == "photos":
        # Visualizzazione con foto
        bot.send_message(chat_id, f"🖼️ *Galleria di {name}* ({len(gallery)} foto):")
        
        for idx, sighting in enumerate(gallery, 1):
            username = sighting['username'] or sighting['first_name'] or "Utente sconosciuto"
            caption = f"Foto {idx}/{len(gallery)}\nSegnalata da: {username}\nData: {sighting['timestamp']}"
            
            try:
                bot.send_photo(
                    chat_id, 
                    photo=sighting["file_id"],
                    caption=caption
                )
            except Exception as e:
                logger.error(f"Errore invio foto: {str(e)}")
    
    del pending_gallery_matto[chat_id]
    bot.answer_callback_query(call.id)

@bot.message_handler(commands=["upload_matti"])
def cmd_upload_matti(msg: types.Message):
    global admin_upload_pending
    if msg.chat.id != ADMIN_CHAT_ID:
        bot.send_message(msg.chat.id, "❌ Comando riservato all'admin!")
        return
    
    admin_upload_pending = True
    bot.send_message(
        msg.chat.id, 
        "📄 Invia ora il file `.txt` con la lista \\(ogni riga: `nome, punti`\\)\\.",
        parse_mode="MarkdownV2"
    )

@bot.message_handler(content_types=["document"])
def handler_document(msg: types.Message):
    global admin_upload_pending
    if msg.chat.id != ADMIN_CHAT_ID or not admin_upload_pending:
        return
    
    doc = msg.document
    if not doc.file_name.lower().endswith(".txt"):
        bot.send_message(msg.chat.id, "❌ Per favore invia un file di testo `.txt`.", parse_mode="Markdown")
        admin_upload_pending = False
        return
    
    try:
        file_info = bot.get_file(doc.file_id)
        content = bot.download_file(file_info.file_path).decode("utf-8")
        
        # Crea file temporaneo sicuro
        with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        count = load_matti_from_file(tmp_path)
        os.unlink(tmp_path)  # Cancella file temporaneo
        
        admin_upload_pending = False
        bot.send_message(msg.chat.id, f"✅ Caricati {count} matti nel database.")
        
    except Exception as e:
        logger.error(f"Errore caricamento matti: {str(e)}")
        bot.send_message(msg.chat.id, f"❌ Errore durante il caricamento: {str(e)}")
        admin_upload_pending = False

# ————— FLOW /report → selezione matto → foto —————
@bot.message_handler(commands=["report"])
def cmd_report(msg: types.Message):
    chat_id = msg.chat.id
    with db_lock:
        row = CUR.execute(
            "SELECT registered FROM users WHERE chat_id = ?;", 
            (chat_id,)
        ).fetchone()
    
    if not row or row['registered'] != 1:
        bot.send_message(chat_id, "❌ Devi prima registrarti con /start.")
        return
    
    items = list_matti()
    if not items:
        bot.send_message(
            chat_id, 
            "📂 Nessun matto definito\\. L'admin può caricarli con `/upload_matti`\\.",
            parse_mode="MarkdownV2"
        )
        return
    
    markup = InlineKeyboardMarkup(row_width=2)
    for itm in items:
        markup.add(InlineKeyboardButton(
            text=f"{itm['name']} (+{itm['points']})",
            callback_data=f"matto|{itm['id']}"
        ))
    
    bot.send_message(
        chat_id, 
        "🏹 Scegli il matto cliccando sul pulsante:", 
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("matto|"))
def callback_matto(call: types.CallbackQuery):
    chat_id = call.from_user.id
    parts = call.data.split("|", 1)
    
    if len(parts) < 2 or not parts[1].isdigit():
        bot.answer_callback_query(call.id, "ID non valido!", show_alert=True)
        return
    
    matto_id = int(parts[1])
    with db_lock:
        row = CUR.execute(
            "SELECT name, points FROM matti WHERE id = ?;", 
            (matto_id,)
        ).fetchone()
    
    if not row:
        bot.answer_callback_query(call.id, "Matto non trovato!", show_alert=True)
        return
    
    name = row["name"]
    pts = row["points"]
    pending_matto[chat_id] = {
        "id": matto_id, 
        "name": name, 
        "points": pts,
        "first_name": call.from_user.first_name or "",
        "username": call.from_user.username or ""
    }
    
    bot.answer_callback_query(call.id, f"Hai scelto: {name} (+{pts})")
    bot.send_message(
        chat_id, 
        f"Hai scelto *{name}* \\(\\+{pts} punti\\)\\.\nAdesso inviami la *foto*\\.",
        parse_mode="MarkdownV2"
    )

@bot.message_handler(content_types=["photo"])
def handler_photo(msg: types.Message):
    chat_id = msg.chat.id
    if chat_id not in pending_matto:
        return
    
    info = pending_matto.pop(chat_id)
    matto_id = info["id"]
    name = info["name"]
    pts = info["points"]
    first = info["first_name"]
    uname = info["username"]
    file_id = msg.photo[-1].file_id
    
    add_sighting(chat_id, matto_id, pts, file_id)
    
    with db_lock:
        total_pts = CUR.execute(
            "SELECT total_points FROM users WHERE chat_id = ?;", 
            (chat_id,)
        ).fetchone()["total_points"]
    
    text = (
        f"📸 *{first}* \\(@{uname}\\) ha trovato il matto *{name}* ➕ *{pts} punti*\n"
        f"🏅 Ora ha *{total_pts} punti*\\."
    )
    
    sent = 0
    removed = []
    registered_ids = get_registered_chat_ids()
    
    for cid in registered_ids:
        try:
            bot.send_message(cid, text, parse_mode="MarkdownV2")
            bot.send_photo(cid, photo=file_id)
            sent += 1
        except ApiException as e:
            error_msg = str(e).lower()
            if any(kw in error_msg for kw in ("blocked", "not found", "deactivated")):
                logger.info(f"Disregistro utente {cid} (inattivo)")
                unregister_user(cid)
                removed.append(cid)
            else:
                logger.error(f"Errore invio a {cid}: {error_msg}")
        except Exception as e:
            logger.error(f"Errore generico invio a {cid}: {str(e)}")
    
    bot.send_message(
        chat_id, 
        f"✅ Segnalazione inviata a {sent - len(removed)} utenti."
    )

# ————— AVVIO BOT —————
if __name__ == "__main__":
    init_db()
    logger.info("Bot avviato – in attesa di comandi.")
    bot.infinity_polling()
