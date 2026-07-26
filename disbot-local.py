import asyncio
import base64
import os
import re
import json
import random
from datetime import timedelta, datetime, timezone
import time
from collections import defaultdict
import requests
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

TOKEN = os.getenv("TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
if not TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Missing TOKEN or OPENROUTER_API_KEY")
# === PER-SERVER CONFIG ===
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_configs.json")
_config_cache = None
_levels_cache = None
_economy_cache = None
_economy_dirty = False
_levels_dirty = False

DEFAULT_CONFIG = {
    "chatbot_enabled": True,
    "safety_enabled": True,
    "ping_spam_enabled": True,
    "blacklist_enabled": True,
    "auto_role_enabled": True,
    "ping_window": 10,
    "ping_threshold": 5,
    "timeout_duration": 300,
    "roast_style": "brutal",
    "prefix": "!",
    "rate_limit_seconds": 5,
    "anti_spam_window": 10,
    "anti_spam_threshold": 6,
    "economy_enabled": True,
    "coins_per_message_min": 3,
    "coins_per_message_max": 10,
    "daily_coins_min": 50,
    "daily_coins_max": 150,
}

CONFIG_HELP = {
    "chatbot_enabled": "Whether the bot replies to chat messages (mentions + auto-reply). Turn off to run as a pure moderation bot with no LLM responses.",
    "safety_enabled": "AI safety filter using ling-2.6-flash. Blocks doxxing, addresses, phone numbers, real threats. Allows swearing, jokes, trash talk.",
    "ping_spam_enabled": "Detects and timeouts users who ping-spam. Counts @user, @role, and @everyone pings.",
    "blacklist_enabled": "Censorship filter. Deletes messages containing slurs, NSFW terms, or suicide-bait phrases. Sends a roast in response.",
    "auto_role_enabled": "Automatically assigns the 'Member' role to new users when they join the server.",
    "ping_window": "Time window in seconds for counting pings. Pings older than this are ignored.",
    "ping_threshold": "Number of pings within the window that triggers a timeout.",
    "timeout_duration": "Base timeout duration in seconds. Scales with repeat offenses: offense #1 = 1x, #2 = 2x, up to 5x.",
    "roast_style": "Tone of roast messages. Options: brutal (harsh), sarcastic (dry wit), wholesome (playful teasing), off (no roasts, silent delete/block).",
    "prefix": "Command prefix for this server. Default: ! (not yet implemented).",
    "rate_limit_seconds": "Cooldown in seconds before the bot responds to the same user again. Prevents API credit drain from spam.",
    "anti_spam_window": "Time window in seconds for message spam detection.",
    "anti_spam_threshold": "Number of messages in the window that triggers a spam timeout.",
    "economy_enabled": "Enable economy system — coin earning, !balance, !pay, !daily, !shop.",
    "coins_per_message_min": "Minimum random coins earned per message (with XP cooldown).",
    "coins_per_message_max": "Maximum random coins earned per message (with XP cooldown).",
    "daily_coins_min": "Minimum coins from !daily command.",
    "daily_coins_max": "Maximum coins from !daily command.",
}

def _load_config():
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        with open(CONFIG_FILE, "r") as f:
            _config_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _config_cache = {"defaults": {}, "guilds": {}}
    return _config_cache

def _save_config(cfg):
    global _config_cache
    _config_cache = cfg
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)

def get_guild_config(guild_id, key):
    cfg = _load_config()
    guild_id = str(guild_id)
    if guild_id in cfg.get("guilds", {}) and key in cfg["guilds"][guild_id]:
        return cfg["guilds"][guild_id][key]
    if key in cfg.get("defaults", {}):
        return cfg["defaults"][key]
    return DEFAULT_CONFIG[key]

def set_guild_config(guild_id, key, value):
    cfg = _load_config()
    guild_id = str(guild_id)
    if "guilds" not in cfg:
        cfg["guilds"] = {}
    if guild_id not in cfg["guilds"]:
        cfg["guilds"][guild_id] = {}
    cfg["guilds"][guild_id][key] = value
    _save_config(cfg)

def reset_guild_config(guild_id):
    cfg = _load_config()
    cfg.get("guilds", {}).pop(str(guild_id), None)
    _save_config(cfg)


LM_STUDIO_URL = "https://openrouter.ai/api/v1"
MODEL_NAME = "google/gemini-3.5-flash-lite"
LLM_REQUEST_TIMEOUT = 60
MAX_PROMPT_LENGTH = 2000

DEFAULT_SYSTEM_PROMPT = "You're in a Discord group chat. Messages from users show up as \"name: message\" — that's just how you see them, don't mimic that format in your replies. Just respond like a normal person in the chat. Don't act like a corporate chatbot -- no \"I'm here to help!\" energy. No bullet points unless specifically asked for a list. Keep it casual, direct, don't over-explain. Match the vibe of whoever you're talking to. Never mention you're an AI or LLM unless asked directly."

client = OpenAI(base_url=LM_STUDIO_URL, api_key=OPENROUTER_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
# Limit concurrent API calls so commands stay responsive
_api_semaphore = asyncio.Semaphore(3)

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")


# --- GAME VIEWS ---

class TTTButton(discord.ui.Button):
    def __init__(self, pos):
        super().__init__(style=discord.ButtonStyle.secondary, label="　", row=pos // 3)
        self.pos = pos

    async def callback(self, interaction: discord.Interaction):
        view: "TicTacToeView" = self.view
        if interaction.user.id != view.players[view.turn]:
            await interaction.response.send_message("Not your turn!", ephemeral=True)
            return
        if view.board[self.pos] is not None:
            await interaction.response.send_message("Taken!", ephemeral=True)
            return
        mark = "❌" if view.turn == 0 else "⭕"
        view.board[self.pos] = mark
        self.label = mark
        self.style = discord.ButtonStyle.danger if mark == "❌" else discord.ButtonStyle.success
        self.disabled = True
        winner = view._check_winner()
        if winner:
            for b in view.children:
                b.disabled = True
            view.stop()
            if view.bet:
                wid = view.players[0] if winner == "❌" else view.players[1]
                add_coins(view.guild_id, wid, view.bet * 2)
            await interaction.response.edit_message(content=f"{view._names()}\n🏆 **{interaction.user.display_name}** wins!", view=view)
        elif all(view.board):
            for b in view.children:
                b.disabled = True
            view.stop()
            if view.bet:
                add_coins(view.guild_id, view.players[0], view.bet)
                add_coins(view.guild_id, view.players[1], view.bet)
            await interaction.response.edit_message(content=f"{view._names()} — 🤝 Draw!", view=view)
        else:
            view.turn = 1 - view.turn
            await interaction.response.edit_message(content=view._names(), view=view)


class TicTacToeView(discord.ui.View):
    def __init__(self, p1, p2, bet=0, guild_id=0):
        super().__init__(timeout=120)
        self.players = (p1.id, p2.id)
        self.p1_name = p1.display_name
        self.p2_name = p2.display_name
        self.bet = bet
        self.guild_id = guild_id
        self.board = [None] * 9
        self.turn = 0
        for i in range(9):
            self.add_item(TTTButton(i))

    def _names(self):
        return f"❌ **{self.p1_name}** vs ⭕ **{self.p2_name}**"

    def _check_winner(self):
        wins = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]
        for a, b, c in wins:
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    async def on_timeout(self):
        for b in self.children:
            b.disabled = True
        if self.bet:
            add_coins(self.guild_id, self.players[0], self.bet)
            add_coins(self.guild_id, self.players[1], self.bet)


class Connect4Button(discord.ui.Button):
    def __init__(self, col):
        super().__init__(style=discord.ButtonStyle.primary, label=f"▾{col+1}", row=0)
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        view: "Connect4View" = self.view
        if interaction.user.id != view.players[view.turn]:
            await interaction.response.send_message("Not your turn!", ephemeral=True)
            return
        for row in range(5, -1, -1):
            if view.board[row][self.col] is None:
                view.board[row][self.col] = view.turn
                break
        else:
            await interaction.response.send_message("Column full!", ephemeral=True)
            return
        view.turn = 1 - view.turn
        winner = view._check_winner()
        if winner is not None:
            for b in view.children:
                b.disabled = True
            view.stop()
            wid = view.players[winner]
            wname = view.p1_name if winner == 0 else view.p2_name
            if view.bet:
                add_coins(view.guild_id, wid, view.bet * 2)
            await interaction.response.edit_message(content=f"{view._render()}\n🏆 **{wname}** wins!", view=view)
        elif all(view.board[0]):
            for b in view.children:
                b.disabled = True
            view.stop()
            if view.bet:
                add_coins(view.guild_id, view.players[0], view.bet)
                add_coins(view.guild_id, view.players[1], view.bet)
            await interaction.response.edit_message(content=f"{view._render()} — 🤝 Draw!", view=view)
        else:
            await interaction.response.edit_message(content=view._render(), view=view)


class Connect4View(discord.ui.View):
    def __init__(self, p1, p2, bet=0, guild_id=0):
        super().__init__(timeout=180)
        self.players = (p1.id, p2.id)
        self.p1_name = p1.display_name
        self.p2_name = p2.display_name
        self.bet = bet
        self.guild_id = guild_id
        self.board = [[None] * 7 for _ in range(6)]
        self.turn = 0
        for i in range(7):
            self.add_item(Connect4Button(i))

    def _render(self):
        emoji = {0: "🔴", 1: "🟡", None: "⬛"}
        rows = ["".join(emoji[cell] for cell in row) for row in self.board]
        rows.append("1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣")
        return f"🔴 **{self.p1_name}** vs 🟡 **{self.p2_name}**\n" + "\n".join(rows)

    def _check_winner(self):
        # horizontal, vertical, diagonal
        for r in range(6):
            for c in range(7):
                cell = self.board[r][c]
                if cell is None:
                    continue
                if c <= 3 and all(self.board[r][c+i] == cell for i in range(4)):
                    return cell
                if r <= 2 and all(self.board[r+i][c] == cell for i in range(4)):
                    return cell
                if r <= 2 and c <= 3 and all(self.board[r+i][c+i] == cell for i in range(4)):
                    return cell
                if r >= 3 and c <= 3 and all(self.board[r-i][c+i] == cell for i in range(4)):
                    return cell
        return None

    async def on_timeout(self):
        for b in self.children:
            b.disabled = True
        if self.bet:
            add_coins(self.guild_id, self.players[0], self.bet)
            add_coins(self.guild_id, self.players[1], self.bet)


# --- COMMANDS ---

chat_history = {}
system_prompts = {}
auto_reply_enabled = {}

# --- ANTI-SPAM PING MODERATION ---
_ping_log = defaultdict(list)
PING_WINDOW = 10
PING_THRESHOLD = 5
_TIMEOUT_DURATIONS = [60, 120, 300, 600, 1800]
_timeout_offenses = defaultdict(lambda: (0, 0))
_TIMEOUT_COOLDOWN = 5  # seconds to ignore pings after a timeout
_timeout_cooldown = {}

# --- RATE LIMIT ---
_last_message_time = {}  # {user_id: timestamp}
_last_xp_time = {}  # {user_id: timestamp} for XP cooldown
_msg_log = defaultdict(list)  # {user_id: [timestamps]} for anti-spam

# --- WORD BLACKLIST ---
BLACKLISTED_WORDS = {
    # racial slurs
    "nigger", "nigga", "niggas", "kike", "chink", "spic", "gook",
    "wetback", "coon", "jap", "heeb", "raghead", "sandnigger",
    "tranny", "shemale", "faggot", "fag", "dyke",
    # nsfw / explicit
    "porn", "xxx", "onlyfans", "nsfw", "tits", "boobs", "cock",
    "dick", "pussy", "cunt", "cum", "penis", "vagina", "asshole",
    "bastard", "bitch", "whore", "slut", "dildo", "vibrator",
    "hentai", "masturbate", "masturbation", "orgasm", "ejaculate",
    "blowjob", "handjob", "sex", "intercourse", "genitals",
    "testicles", "clitoris", "nipple", "nipples", "anal",
    "doggystyle", "missionary", "cumshot", "creampie", "bukkake",
}
# words that are fine standalone but not in combination
BLACKLISTED_PHRASES = [
    "kill yourself", "kill urself", "kys", "kill urself",
    "hang yourself", "end your life", "suicide bait",
]

def _check_blacklist(message):
    content_lower = message.content.lower()
    words = set(re.findall(r'[a-zA-Z]+', content_lower))
    hits = words & BLACKLISTED_WORDS
    for phrase in BLACKLISTED_PHRASES:
        if phrase in content_lower:
            hits.add(phrase)
    return hits

# --- LEVELING SYSTEM ---
LEVEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "levels.json")
XP_PER_MESSAGE = 10
XP_COOLDOWN = 10

def _load_levels():
    global _levels_cache
    if _levels_cache is not None:
        return _levels_cache
    try:
        with open(LEVEL_FILE, "r") as f:
            _levels_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _levels_cache = {}
    return _levels_cache

def _save_levels(data):
    global _levels_cache, _levels_dirty
    _levels_cache = data
    _levels_dirty = True

# --- Language auto-translate (local NLLB-200 via CTranslate2) ---
import ctranslate2
from ftlangdetect import detect as ft_detect
LANG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "langs.json")
_langs_cache = None

# ISO 639-1 → NLLB FLORES-200 code
LANG_TO_NLLB = {
    "en": "eng_Latn", "tl": "tgl_Latn", "fil": "tgl_Latn",
    "ja": "jpn_Jpan", "ko": "kor_Hang", "zh": "zho_Hans",
    "fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn",
    "pt": "por_Latn", "it": "ita_Latn", "ru": "rus_Cyrl",
    "ar": "arb_Arab", "hi": "hin_Deva", "th": "tha_Thai",
    "vi": "vie_Latn", "id": "ind_Latn", "ms": "zsm_Latn",
    "nl": "nld_Latn", "pl": "pol_Latn", "tr": "tur_Latn",
    "sv": "swe_Latn", "da": "dan_Latn", "fi": "fin_Latn",
    "no": "nob_Latn", "cs": "ces_Latn", "el": "ell_Grek",
    "he": "heb_Hebr", "hu": "hun_Latn", "ro": "ron_Latn",
    "uk": "ukr_Cyrl", "bg": "bul_Cyrl", "ca": "cat_Latn",
}
# Reverse: NLLB code → friendly name
NLLB_NAMES = {v: k for k, v in LANG_TO_NLLB.items()}

_nllb_translator = None
_nllb_tokenizer = None
_lang_token_cache = {}  # nllb_code → [token_string]

def _init_nllb():
    global _nllb_translator, _nllb_tokenizer
    if _nllb_translator is None:
        model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nllb-200-ct2-int8")
        _nllb_translator = ctranslate2.Translator(model_dir, device="cpu", intra_threads=4)
        import transformers
        _nllb_tokenizer = transformers.AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M")
        print("[translate] NLLB-200 loaded (CTranslate2 INT8)")
    return _nllb_translator, _nllb_tokenizer

def _load_langs():
    global _langs_cache
    if _langs_cache is not None:
        return _langs_cache
    try:
        with open(LANG_FILE, "r") as f:
            _langs_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _langs_cache = {}
    return _langs_cache

def _save_langs(data):
    global _langs_cache
    _langs_cache = data
    tmp = LANG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LANG_FILE)

def _iso_to_nllb(iso_code):
    """Convert user-set lang code (e.g. 'tl', 'ja') to NLLB FLORES-200 code."""
    iso = iso_code.lower().strip()
    return LANG_TO_NLLB.get(iso, iso_code)  # fallback: use as-is

def _detect_lang(text):
    """Fast language detection. Returns ISO 639-1 code or None."""
    try:
        result = ft_detect(text=text, low_memory=True)
        return result["lang"]
    except Exception:
        return None

_translate_semaphore = asyncio.Semaphore(2)
_translation_cache = {}

def _get_lang_token(nllb_code):
    """Get target_prefix token list for a NLLB language code."""
    if nllb_code in _lang_token_cache:
        return _lang_token_cache[nllb_code]
    _, tokenizer = _init_nllb()
    tid = tokenizer.convert_tokens_to_ids(nllb_code)
    if tid == tokenizer.unk_token_id:
        return None
    token = tokenizer.convert_ids_to_tokens([tid])[0]
    _lang_token_cache[nllb_code] = [token]
    return [token]

def _sync_translate(text, source_nllb, target_nllb):
    """Blocking CTranslate2 translation call — run in thread."""
    translator, tokenizer = _init_nllb()
    target_prefix = _get_lang_token(target_nllb)
    if target_prefix is None:
        return None
    tokenizer.src_lang = source_nllb
    tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode(text, truncation=True, max_length=256))
    results = translator.translate_batch(
        [tokens],
        target_prefix=[target_prefix],
        beam_size=1,
        max_decoding_length=512,
    )
    return tokenizer.decode(tokenizer.convert_tokens_to_ids(results[0].hypotheses[0]), skip_special_tokens=True)

async def _translate_if_needed(text, target_nllb):
    """Detect lang + translate via local NLLB-200. Returns None if same language."""
    if not text.strip():
        return None
    detected = _detect_lang(text)
    if detected is None:
        return None
    source_nllb = _iso_to_nllb(detected)
    # Map detected to a short prefix for comparison (e.g. 'eng' vs 'eng')
    if source_nllb[:3] == target_nllb[:3]:
        return None  # same language family
    try:
        async with _translate_semaphore:
            result = await asyncio.to_thread(_sync_translate, text, source_nllb, target_nllb)
        return result if result and result != text else None
    except Exception as e:
        print(f"[translate:err] {e}")
        return None

async def _maybe_translate(message):
    """Check guild members' lang prefs and send spoiler-tagged translations."""
    try:
        if not message.guild or not message.content or not message.content.strip():
            return
        if message.content.startswith(tuple(bot.command_prefix) if isinstance(bot.command_prefix, str) else bot.command_prefix):
            return
        if message.content.startswith(("!", "?", ".", "/", "-", ";")):
            return
        langs = _load_langs()
        if not langs:
            return
        target_langs = set()
        lang_users = {}
        author_uid = str(message.author.id)
        for uid_str, lang in langs.items():
            if uid_str == author_uid:
                continue
            uid = int(uid_str)
            member = message.guild.get_member(uid)
            if member and not member.bot:
                target_langs.add(lang)
                lang_users.setdefault(lang, []).append(uid)
        if not target_langs:
            return
        content = message.content[:1000]
        content_hash = hash(content)
        for target_iso in target_langs:
            target_nllb = _iso_to_nllb(target_iso)
            cache_key = (content_hash, target_nllb)
            if cache_key in _translation_cache:
                translated = _translation_cache[cache_key]
            else:
                translated = await _translate_if_needed(content, target_nllb)
                _translation_cache[cache_key] = translated
                if len(_translation_cache) > 200:
                    _translation_cache.pop(next(iter(_translation_cache)))
            if translated:
                print(f"[translate] {message.author.name} → {target_iso}: {translated[:60]!r}")
                for uid in lang_users[target_iso]:
                    try:
                        user = message.guild.get_member(uid) or await message.guild.fetch_member(uid)
                        if user:
                            await message.channel.send(
                                f"{user.mention} ||**{message.author.display_name}**: {translated[:1500]}||",
                                silent=True,
                            )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
    except Exception as e:
        print(f"[translate:err] {e}")

def get_level(xp):
    # Level 5 = 200 XP (20 msgs), Level 20 = 3200 XP (320 msgs), Level 50 = 20000 XP (2000 msgs)
    return max(0, int((xp / 8) ** 0.5))

def xp_for_level(level):
    return 8 * (level ** 2)

def add_xp(guild_id, user_id, xp_override=None):
    levels = _load_levels()
    gid = str(guild_id)
    uid = str(user_id)
    if gid not in levels:
        levels[gid] = {}
    if uid not in levels[gid]:
        levels[gid][uid] = {"xp": 0}
    xp_gain = xp_override if xp_override is not None else XP_PER_MESSAGE
    old_level = get_level(levels[gid][uid]["xp"])
    levels[gid][uid]["xp"] += xp_gain
    new_level = get_level(levels[gid][uid]["xp"])
    _save_levels(levels)
    return new_level > old_level, new_level

# --- ECONOMY SYSTEM ---
ECO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "economy.json")

SHOP_ITEMS = {
    "lootbox": {"name": "🎁 Loot Box", "price": 200, "desc": "Random 50-500 coins"},
    "scratchie": {"name": "🎫 Scratchie", "price": 100, "desc": "Win 0–500 coins instantly"},
    "megaphone": {"name": "📢 Megaphone", "price": 500, "desc": "Your next message gets pinned"},
    "spotlight": {"name": "💡 Spotlight", "price": 1000, "desc": "Your next message gets an announcement embed"},
    "xpboost": {"name": "⚡ XP Boost", "price": 400, "desc": "2x XP for 1 hour"},
    "coinmagnet": {"name": "🧲 Coin Magnet", "price": 350, "desc": "2x coin earnings for 1 hour"},
    "padlock": {"name": "🔒 Padlock", "price": 300, "desc": "Blocks !rob against you for 24h"},
}

SLOT_EMOJIS = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 15, 7, 3]
COINFLIP_MULTIPLIER = 1.9
BJ_BUST = 21

def _load_economy():
    global _economy_cache
    if _economy_cache is not None:
        return _economy_cache
    try:
        with open(ECO_FILE, "r") as f:
            _economy_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _economy_cache = {}
    return _economy_cache

def _save_economy(data):
    global _economy_cache, _economy_dirty
    _economy_cache = data
    _economy_dirty = True

def get_coins(guild_id, user_id):
    eco = _load_economy()
    return eco.get(str(guild_id), {}).get(str(user_id), {}).get("coins", 0)

def add_coins(guild_id, user_id, amount):
    eco = _load_economy()
    gid, uid = str(guild_id), str(user_id)
    eco.setdefault(gid, {}).setdefault(uid, {"coins": 0})
    eco[gid][uid]["coins"] += amount
    _save_economy(eco)

def remove_coins(guild_id, user_id, amount):
    eco = _load_economy()
    gid, uid = str(guild_id), str(user_id)
    if gid not in eco or uid not in eco[gid]:
        return False
    if eco[gid][uid].get("coins", 0) < amount:
        return False
    eco[gid][uid]["coins"] -= amount
    _save_economy(eco)
    return True

def has_active_item(guild_id, user_id, item_key):
    eco = _load_economy()
    items = eco.get(str(guild_id), {}).get(str(user_id), {}).get("active_items", {})
    if item_key == "xpboost":
        return items.get("xpboost", 0) > time.time()
    return items.get(item_key, False)

def consume_item(guild_id, user_id, item_key):
    eco = _load_economy()
    gid, uid = str(guild_id), str(user_id)
    eco.setdefault(gid, {}).setdefault(uid, {}).setdefault("active_items", {})
    eco[gid][uid]["active_items"][item_key] = False
    _save_economy(eco)

def activate_item(guild_id, user_id, item_key):
    eco = _load_economy()
    gid, uid = str(guild_id), str(user_id)
    eco.setdefault(gid, {}).setdefault(uid, {}).setdefault("active_items", {})
    if item_key == "xpboost":
        eco[gid][uid]["active_items"]["xpboost"] = time.time() + 3600
    else:
        eco[gid][uid]["active_items"][item_key] = True
    _save_economy(eco)

_pending_flips = {}  # {challenged_id: {"challenger": id, "bet": int, "guild": id, "choice": str}}
_bj_games = {}  # {user_id: {"bet": int, "guild": id, "player": [...], "dealer": [...], "deck": [...]}}
_hilo_games = {}  # {user_id: {"bet": int, "guild": id, "card": int, "streak": int, "deck": [...]}}
RACE_ANIMALS = {"🐎": (40, 2), "🐕": (30, 3), "🐈": (20, 4), "🐓": (10, 8)}  # emoji: (weight, payout)

INTEREST_RATE = 0.02  # 2% daily
INTEREST_CAP = 500
ROB_SUCCESS = 0.4  # 40% chance
ROB_PCT_MIN = 0.05  # steal 5% min
ROB_PCT_MAX = 0.25  # steal 25% max
ROB_FINE = 200  # fine if caught

def _card_str(card):
    suits = {"♠": "♠", "♥": "♥", "♦": "♦", "♣": "♣"}
    faces = {1: "A", 11: "J", 12: "Q", 13: "K"}
    val, suit = card
    name = faces.get(val, str(val))
    return f"{name}{suit}"

def _hand_value(hand):
    total, aces = 0, 0
    for val, _ in hand:
        if val == 1:
            aces += 1
            total += 11
        elif val > 10:
            total += 10
        else:
            total += val
    while total > BJ_BUST and aces > 0:
        total -= 10
        aces -= 1
    return total

def _new_deck():
    import random as _r
    deck = [(v, s) for s in ["♠", "♥", "♦", "♣"] for v in range(1, 14)]
    _r.shuffle(deck)
    return deck

SAFETY_MODEL = "inclusionai/ling-2.6-flash"
SAFETY_SYSTEM = """You are a content moderator. Reply ONLY 'yes' or 'no'.

Reply 'no' ONLY if the message contains: real phone numbers, real street addresses, dox/doxxing, "kill yourself"/"kys", suicide encouragement, CSAM, or gore imagery descriptions.

Reply 'yes' for EVERYTHING else — including: swearing, insults, arguments, trash talk, "i'll kill you", "shut up", dark humor, sarcasm, roasts, bot discussion, debugging, hypotheticals, URLs, gambling discussion, rule arguments, admin/mod discussion.

CRITICAL: Do NOT flag messages about rules, policies, or moderation. Do NOT flag IP addresses or URLs unless they are clearly doxxing someone. When in doubt, reply 'yes'."""

async def _check_safety(message):
    if not message.content or not message.content.strip():
        return False  # skip images/attachments with no text
    content = message.content[:500]
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": SAFETY_MODEL,
                    "messages": [
                        {"role": "system", "content": SAFETY_SYSTEM},
                        {"role": "user", "content": f"Message from {message.author.display_name}: {content}"},
                    ],
                    "max_tokens": 2,
                    "temperature": 0,
                },
                timeout=10,
            ),
            timeout=12,
        )
        data = resp.json()
        answer = data["choices"][0]["message"]["content"].strip().lower()
        flagged = "no" in answer and "yes" not in answer
        if flagged:
            print(f"[safety] flagged: {message.author.name} — {content[:80]}")
        return flagged
    except Exception as e:
        print(f"[safety] API error: {e}")
        return False  # if API fails, let the message through

async def _check_ping_spam(message):
    if not message.guild:
        return
    author = message.author
    if author.bot:
        return

    # skip pings during post-timeout cooldown
    if time.time() - _timeout_cooldown.get(author.id, 0) < _TIMEOUT_COOLDOWN:
        return

    ping_count = message.content.count("<@")
    if ping_count <= 0:
        return

    now = time.time()
    for _ in range(ping_count):
        _ping_log[author.id].append(now)

    cutoff = now - PING_WINDOW
    _ping_log[author.id] = [t for t in _ping_log[author.id] if t > cutoff]
    total = len(_ping_log[author.id])
    print(f"[mod:dbg] total pings in window: {total}/{PING_THRESHOLD}")

    if total >= PING_THRESHOLD:
        last_ts, offense_n = _timeout_offenses[author.id]
        if now - last_ts > 300:
            offense_n = 0
        offense_n += 1
        base_duration = get_guild_config(message.guild.id, "timeout_duration")
        offense_n = min(offense_n, 5)
        duration = base_duration * offense_n
        _timeout_offenses[author.id] = (now, offense_n)

        # lock immediately so concurrent pings don't re-trigger during the LLM call
        _ping_log[author.id].clear()
        _timeout_cooldown[author.id] = time.time()

        try:
            print(f"[mod:dbg] attempting timeout: {author.name} for {duration}s")
            await author.timeout(
                discord.utils.utcnow() + timedelta(seconds=duration),
                reason=f"Ping spam ({total} pings in {PING_WINDOW}s)"
            )
            roast = await get_llm_response(
                message.channel.id,
                f"Someone just got timed out for ping spamming ({duration}s, offense #{offense_n}). Call them out with a short, {get_guild_config(message.guild.id, "roast_style")} one-liner roast. Max 150 chars. Their name: {author.display_name}",
                "system",
                None
            )
            await message.channel.send(f"{author.mention} {roast}")
            print(f"[mod:dbg] timeout SUCCESS for {author.name}")
        except discord.Forbidden:
            print(f"[mod] NO PERMS to timeout {author.name}")
            roast = await get_llm_response(
                message.channel.id,
                f"Someone ping-spammed but is immune to timeout. Mock them with a short, funny one-liner about being untouchable. Max 150 chars. Their name: {author.display_name}",
                "system",
                None
            )
            try:
                await message.channel.send(f"{author.mention} {roast}")
            except Exception:
                pass
        except discord.HTTPException as e:
            print(f"[mod] timeout HTTP error for {author.name}: {e}")
    else:
        print(f"[mod] {author.name} +{ping_count} ping(s) \u2192 {total}/{PING_THRESHOLD}")


# --- LLM HELPERS ---

IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
AUDIO_MIMES = {"audio/ogg", "audio/mp3", "audio/wav", "audio/mpeg", "audio/webm", "audio/opus"}

def _download_attachment(url):
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.content[:10_000_000]
    mime = r.headers.get("content-type", "application/octet-stream")
    return mime, base64.b64encode(data).decode()

def _sync_llm_response(channel_id, user_input, user_name, attachments=None):
    sys_msg = system_prompts.get(channel_id, DEFAULT_SYSTEM_PROMPT)
    if channel_id not in chat_history:
        chat_history[channel_id] = [{"role": "system", "content": sys_msg}]

    user_content = [{"type": "text", "text": f"{user_name}: {user_input}"}]

    if attachments:
        for att in attachments:
            try:
                mime, b64 = _download_attachment(att.url)
                if mime in IMAGE_MIMES:
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}
                    })
                elif mime in AUDIO_MIMES:
                    user_content.append({
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": mime.split("/")[-1]}
                    })
                else:
                    user_content.append({
                        "type": "text",
                        "text": f"[attached file: {att.filename} ({att.content_type or 'unknown'})]"
                    })
            except Exception as e:
                user_content.append({
                    "type": "text",
                    "text": f"[failed to download {att.filename}: {e}]"
                })

    msg = {"role": "user", "content": user_content if len(user_content) > 1 else user_content[0]["text"]}
    chat_history[channel_id].append(msg)

    if len(chat_history[channel_id]) > 50:
        chat_history[channel_id] = [chat_history[channel_id][0]] + chat_history[channel_id][-10:]

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=chat_history[channel_id],
        temperature=0.7,
        max_tokens=500,
    )
    answer = response.choices[0].message.content
    chat_history[channel_id].append({"role": "assistant", "content": answer})
    return answer

async def get_llm_response(channel_id, user_input, user_name, attachments=None):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_llm_response, channel_id, user_input, user_name, attachments),
            timeout=LLM_REQUEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return "took too long, try again"
    except Exception as e:
        return f"error: {e}"

async def send_split_response(target, text):
    if len(text) <= 2000:
        await target.reply(text)
        return
    for i in range(0, len(text), 2000):
        await target.channel.send(text[i:i+2000])


# --- COMMANDS ---

@bot.command(name="help")
async def help_cmd(ctx):
    """Show all commands."""
    lines = [
        "**Chat** — `!ask <q>` `!prompt <rules>` `!clear` `!auto-reply on/off` `!afk [reason]`",
        "**Mod** — `!purge [@user] <count>` `!config [key] [value]` `!poll <q> | <opt1> | <opt2>`",
        "**Leveling** — `!rank [@user]` `!leaderboard`",
        "**Economy** — `!balance [@user]` `!pay @user <amt>` `!rich` `!daily` `!work` `!shop` `!buy <item>` `!rob @user` `!duel @user <bet>` `!buyticket [n]` `!lottery`",
        "**Gambling** — `!slots <bet>` `!cf <bet> h/t` `!cf @user <bet>` `!bj <bet>` `!roulette <bet>` `!hilo <bet>` `!race <bet>`",
        "**Radio** — `!tune <preset>` `!stop` `!stations` `!nowplaying` `!volume <1-100000>` `!bassboost <+dB>` `!deepfry <1-500>` `!bitcrush <1-100>` `!share`",
        "**Util** — `!remind <time> <msg>` `!lang <code>`",
        "",
        "`!<command>` with no args shows detailed usage for that command.",
        "`!lang` auto-translates: set with `!lang tl` (Tagalog), `!lang ja` (Japanese), etc. `!lang off` to disable. Messages in other languages get translated inline.",
    ]
    await ctx.send("\n".join(lines))

@bot.command(name="config")
@commands.has_permissions(manage_guild=True)
async def config_cmd(ctx, key: str = "", *, value: str = ""):
    """View or change per-server settings. Usage:
    !config              → show all settings (summary)
    !config <key>        → show current value of a key
    !config <key> <val>  → set a key
    !config help <key>   → detailed explanation of a key
    !config reset        → reset all to defaults"""
    gid = ctx.guild.id if ctx.guild else ctx.channel.id
    valid_keys = list(DEFAULT_CONFIG.keys())

    # !config help <key>
    if key == "help":
        if not value:
            await ctx.send("Usage: `!config help <key>` — shows detailed info about a setting.")
            return
        if value not in valid_keys:
            await ctx.send(f"❌ Unknown key. Valid: {', '.join(f'`{k}`' for k in valid_keys)}")
            return
        d = DEFAULT_CONFIG[value]
        help_text = CONFIG_HELP.get(value, "No help available.")
        await ctx.send(f"**`{value}`** (default: `{d}`)\n{help_text}")
        return

    # !config reset
    if key == "reset":
        reset_guild_config(gid)
        await ctx.send("✅ Config reset to defaults.")
        return

    # !config (no args) — summary table
    if not key:
        server_name = ctx.guild.name if ctx.guild else "DM"
        lines = [f"**{server_name} — Server Config**", ""]
        lines.append("`Key                 Value     Default   Description`")
        lines.append("`────────────────────────────────────────────────────`")
        for k in valid_keys:
            v = get_guild_config(gid, k)
            d = DEFAULT_CONFIG[k]
            changed = " ✎" if v != d else ""
            short = CONFIG_HELP.get(k, "").split(".")[0] + "."
            lines.append(f"`{k:<20} {str(v):<9} {str(d):<9} {short}`")
        lines.append("")
        lines.append("Use `!config help <key>` for details. `!config <key> <value>` to change.")
        await ctx.send("\n".join(lines))
        return

    # !config <key> (no value) — show one
    if key not in valid_keys:
        await ctx.send(f"❌ Unknown key `{key}`. Valid: {', '.join(f'`{k}`' for k in valid_keys)}")
        return
    if not value:
        v = get_guild_config(gid, key)
        d = DEFAULT_CONFIG[key]
        changed = " (custom)" if v != d else " (default)"
        await ctx.send(f"**`{key}`** = `{v}`{changed}\n{CONFIG_HELP.get(key, '')}")
        return

    # !config <key> <value> — set
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        val = value.lower() in ("true", "yes", "on", "1", "enable")
    elif isinstance(default, int):
        try:
            val = int(value)
        except ValueError:
            await ctx.send("❌ Expected a number.")
            return
    else:
        val = value
    if key == "roast_style" and val not in ("brutal", "sarcastic", "wholesome", "off"):
        await ctx.send("❌ roast_style must be: brutal, sarcastic, wholesome, or off")
        return
    set_guild_config(gid, key, val)
    await ctx.send(f"✅ `{key}` → **{val}**")

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
@commands.cooldown(1, 2, commands.BucketType.channel)
async def purge_cmd(ctx, arg1: str = None, arg2: str = None):
    """Delete messages. !purge <count> or !purge @user <count>"""
    if not arg1:
        await ctx.send("Usage: `!purge <count>` or `!purge @user <count>`", delete_after=5)
        return
    try:
        count = int(arg2) if arg2 else int(arg1)
    except ValueError:
        await ctx.send("❌ Count must be a number.", delete_after=3)
        return
    count = min(count, 100)
    await ctx.message.delete()
    if ctx.message.mentions:
        target = ctx.message.mentions[0]
        deleted = await ctx.channel.purge(limit=count, check=lambda m: m.author == target)
    else:
        deleted = await ctx.channel.purge(limit=count)

@bot.command(name="rank")
async def rank_cmd(ctx, member: discord.Member = None):
    """Check level. !rank or !rank @user"""
    member = member or ctx.author
    levels = _load_levels()
    gid = str(ctx.guild.id)
    uid = str(member.id)
    xp = levels.get(gid, {}).get(uid, {}).get("xp", 0)
    level = get_level(xp)
    next_level = level + 1
    xp_needed = xp_for_level(next_level) - xp
    await ctx.send(f"**{member.display_name}** — Level **{level}** ({xp} XP)\nNeed {xp_needed} XP for level {next_level}")

@bot.command(name="leaderboard")
@commands.cooldown(1, 30, commands.BucketType.guild)
async def leaderboard_cmd(ctx):
    """Top 10 by XP. !leaderboard"""
    levels = _load_levels()
    gid = str(ctx.guild.id)
    guild_data = levels.get(gid, {})
    ranked = sorted(guild_data.items(), key=lambda x: x[1]["xp"], reverse=True)[:10]
    if not ranked:
        await ctx.send("No XP data yet!")
        return
    lines = ["**Leaderboard**", ""]
    for i, (uid, data) in enumerate(ranked, 1):
        lvl = get_level(data["xp"])
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else uid
        lines.append(f"{i}. **{name}** — Level {lvl} ({data['xp']} XP)")
    await ctx.send("\n".join(lines))

# --- ECONOMY COMMANDS ---
@bot.command(name="balance", aliases=["bal"])
async def balance_cmd(ctx, member: discord.Member = None):
    """Check coin balance. !balance or !balance @user"""
    member = member or ctx.author
    coins = get_coins(ctx.guild.id, member.id)
    await ctx.send(f"**{member.display_name}** — **{coins}** coins")

@bot.command(name="pay")
async def pay_cmd(ctx, target: discord.Member = None, amount: int = None):
    """Send coins. !pay @user 100"""
    if not target or amount is None:
        await ctx.send("Usage: `!pay @user <amount>`")
        return
    if amount < 1:
        await ctx.send("❌ Amount must be positive.")
        return
    if target == ctx.author:
        await ctx.send("❌ Can't pay yourself.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, amount):
        await ctx.send(f"❌ You don't have **{amount}** coins.")
        return
    add_coins(ctx.guild.id, target.id, amount)
    await ctx.send(f"**{ctx.author.display_name}** paid **{target.display_name}** **{amount}** coins")

@bot.command(name="daily")
@commands.cooldown(1, 86400, commands.BucketType.user)
async def daily_cmd(ctx):
    """Claim daily coins. !daily"""
    min_c = get_guild_config(ctx.guild.id, "daily_coins_min")
    max_c = get_guild_config(ctx.guild.id, "daily_coins_max")
    amount = random.randint(min_c, max_c)
    add_coins(ctx.guild.id, ctx.author.id, amount)
    await ctx.send(f"**{ctx.author.display_name}** claimed **{amount}** daily coins!")

@bot.command(name="shop")
async def shop_cmd(ctx):
    """View shop items. !shop"""
    lines = ["**Shop**", ""]
    for key, item in SHOP_ITEMS.items():
        lines.append(f"**{item['name']}** — {item['price']} coins  `!buy {key}`")
        lines.append(f"　{item['desc']}")
    lines.append("")
    lines.append("`!buy <item>` to purchase")
    await ctx.send("\n".join(lines))

@bot.command(name="buy")
async def buy_cmd(ctx, *, item_name: str = None):
    """Buy a shop item. !buy lootbox"""
    if not item_name:
        await ctx.send("Usage: `!buy <item>` — see `!shop`")
        return
    key = re.sub(r'[ _-]', '', item_name).lower()
    if key not in SHOP_ITEMS:
        await ctx.send(f"❌ Unknown item. Use `!shop`.")
        return
    item = SHOP_ITEMS[key]
    bal = get_coins(ctx.guild.id, ctx.author.id)
    if not remove_coins(ctx.guild.id, ctx.author.id, item["price"]):
        await ctx.send(f"❌ You need **{item['price']}** coins. You have **{bal}**.")
        return
    if key == "lootbox":
        reward = random.randint(50, 500)
        add_coins(ctx.guild.id, ctx.author.id, reward)
        await ctx.send(f"🎁 **{ctx.author.display_name}** opened a loot box and got **{reward}** coins!")
    elif key == "scratchie":
        reward = random.choices([0, 50, 100, 200, 500], weights=[40, 30, 15, 10, 5])[0]
        if reward:
            add_coins(ctx.guild.id, ctx.author.id, reward)
            await ctx.send(f"🎫 **{ctx.author.display_name}** scratched and won **{reward}** coins!")
        else:
            await ctx.send(f"🎫 **{ctx.author.display_name}** scratched... nothing. Better luck next time!")
    else:
        activate_item(ctx.guild.id, ctx.author.id, key)
        tips = {
            "megaphone": "Your next message will be pinned! 📌",
            "spotlight": "Your next message gets a golden embed! ✨",
            "xpboost": "2x XP for 1 hour! ⚡",
            "coinmagnet": "2x coins for 1 hour! 🧲",
            "padlock": "Protected from !rob for 24h! 🔒",
        }
        tip = tips.get(key, "")
        await ctx.send(f"✅ **{ctx.author.display_name}** bought **{item['name']}**!\n{tip}")

# --- GAMBLING ---
@bot.command(name="slots")
@commands.cooldown(1, 3, commands.BucketType.user)
async def slots_cmd(ctx, bet: int = None):
    """Spin the slot machine. !slots <bet>"""
    if bet is None:
        await ctx.send("Usage: `!slots <bet>` — spin the slot machine\n🎰 Pairs 2x | Triple 5x | 💎 10x | 7️⃣ 25x")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, bet):
        await ctx.send(f"❌ You don't have **{bet}** coins.")
        return
    a, b, c = random.choices(SLOT_EMOJIS, weights=SLOT_WEIGHTS, k=3)
    result = f"🎰 | {a} {b} {c} |"
    if a == b == c:
        if a == "7️⃣":
            payout = bet * 25
        elif a == "💎":
            payout = bet * 10
        else:
            payout = bet * 5
        add_coins(ctx.guild.id, ctx.author.id, payout)
        await ctx.send(f"{result}\n🎉 **JACKPOT!** Won **{payout}** coins!")
    elif a == b or b == c or a == c:
        payout = bet * 2
        add_coins(ctx.guild.id, ctx.author.id, payout)
        await ctx.send(f"{result}\n✨ Pair! Won **{payout}** coins!")
    else:
        await ctx.send(f"{result}\n💨 Lost **{bet}** coins.")

@bot.command(name="coinflip", aliases=["cf"])
@commands.cooldown(1, 2, commands.BucketType.user)
async def coinflip_cmd(ctx, arg1=None, arg2=None):
    """Flip a coin. !cf <bet> <h/t> or !cf @user <bet>"""
    if ctx.author.id in _bj_games:
        await ctx.send("❌ Finish your blackjack game first.")
        return
    # vs bot: !cf <bet> <h/t>
    if arg1 and arg2 and arg1.isdigit():
        bet = int(arg1)
        choice = arg2.lower()
        if choice not in ("h", "t", "heads", "tails"):
            await ctx.send("Usage: `!cf <bet> heads/tails` or `!cf @user <bet>`")
            return
        if bet < 1:
            await ctx.send("❌ Bet must be positive.")
            return
        if not remove_coins(ctx.guild.id, ctx.author.id, bet):
            await ctx.send(f"❌ You don't have **{bet}** coins.")
            return
        flip = random.choice(["heads", "tails"])
        if choice[0] == flip[0]:
            payout = int(bet * COINFLIP_MULTIPLIER)
            add_coins(ctx.guild.id, ctx.author.id, payout)
            await ctx.send(f"🪙 **{flip}**! You win **{payout}** coins!")
        else:
            await ctx.send(f"🪙 **{flip}**! You lose **{bet}** coins.")
        return
    # p2p: !cf @user <bet>
    if ctx.message.mentions and arg2 and arg2.isdigit():
        target = ctx.message.mentions[0]
        bet = int(arg2)
        if target == ctx.author:
            await ctx.send("❌ Can't flip against yourself.")
            return
        if bet < 1:
            await ctx.send("❌ Bet must be positive.")
            return
        if not remove_coins(ctx.guild.id, ctx.author.id, bet):
            await ctx.send(f"❌ You don't have **{bet}** coins.")
            return
        if target.id in _pending_flips:
            await ctx.send(f"❌ {target.mention} already has a pending coinflip.")
            add_coins(ctx.guild.id, ctx.author.id, bet)
            return
        _pending_flips[target.id] = {"challenger": ctx.author.id, "bet": bet, "guild": ctx.guild.id}
        await ctx.send(f"🪙 **{ctx.author.display_name}** challenges **{target.display_name}** for **{bet}** coins!\n{target.mention} — `!accept` or `!decline` (30s)")
        async def _expire_flip():
            await asyncio.sleep(30)
            if target.id in _pending_flips:
                add_coins(ctx.guild.id, ctx.author.id, bet)
                del _pending_flips[target.id]
                await ctx.send(f"⏰ Coinflip expired. {ctx.author.mention} got their **{bet}** back.")
        asyncio.create_task(_expire_flip())
        return
    await ctx.send("Usage: `!cf <bet> heads/tails` or `!cf @user <bet>`")

@bot.command(name="accept")
async def accept_cmd(ctx):
    """Accept a coinflip challenge."""
    if ctx.author.id not in _pending_flips:
        await ctx.send("❌ No pending coinflip for you.")
        return
    flip = _pending_flips.pop(ctx.author.id)
    if not remove_coins(ctx.guild.id, ctx.author.id, flip["bet"]):
        # refund challenger
        add_coins(flip["guild"], flip["challenger"], flip["bet"])
        await ctx.send(f"❌ You don't have **{flip['bet']}** coins. Refunded challenger.")
        return
    challenger = ctx.guild.get_member(flip["challenger"])
    result = random.choice(["heads", "tails"])
    winner_id = flip["challenger"] if result == "heads" else ctx.author.id
    loser_id = ctx.author.id if result == "heads" else flip["challenger"]
    payout = flip["bet"] * 2
    add_coins(flip["guild"], winner_id, payout)
    winner = challenger if result == "heads" else ctx.author
    await ctx.send(f"🪙 **{result}!** {winner.mention} wins **{payout}** coins!")

@bot.command(name="decline")
async def decline_cmd(ctx):
    """Decline a coinflip challenge."""
    if ctx.author.id not in _pending_flips:
        await ctx.send("❌ No pending coinflip for you.")
        return
    flip = _pending_flips.pop(ctx.author.id)
    add_coins(flip["guild"], flip["challenger"], flip["bet"])
    await ctx.send(f"❌ {ctx.author.display_name} declined. Refunded **{flip['bet']}** coins.")

@bot.command(name="blackjack", aliases=["bj"])
@commands.cooldown(1, 5, commands.BucketType.user)
async def blackjack_cmd(ctx, bet: int = None):
    """Play blackjack vs bot. !bj <bet>"""
    if bet is None:
        await ctx.send("Usage: `!bj <bet>` — play blackjack vs the bot\n🃏 Blackjack pays 2.5x, win pays 2x, push returns bet.")
        return
    if ctx.author.id in _bj_games:
        await ctx.send("❌ You already have a blackjack game. Use `!hit` or `!stand`.")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, bet):
        await ctx.send(f"❌ You don't have **{bet}** coins.")
        return
    deck = _new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    _bj_games[ctx.author.id] = {"bet": bet, "guild": ctx.guild.id, "player": player, "dealer": dealer, "deck": deck}
    if _hand_value(player) == BJ_BUST:
        payout = int(bet * 2.5)
        add_coins(ctx.guild.id, ctx.author.id, payout)
        await ctx.send(f"🃏 **Blackjack!** {_card_str(player[0])} {_card_str(player[1])} — You win **{payout}** coins!")
        del _bj_games[ctx.author.id]
        return
    await ctx.send(f"🃏 Your hand: {_card_str(player[0])} {_card_str(player[1])} ({_hand_value(player)})\n"
                   f"🂠 Dealer shows: {_card_str(dealer[0])}\n"
                   f"`!hit` or `!stand`")

@bot.command(name="hit")
async def hit_cmd(ctx):
    """Draw a card in blackjack."""
    if ctx.author.id not in _bj_games:
        await ctx.send("❌ No active blackjack game. `!bj <bet>` to start.")
        return
    game = _bj_games[ctx.author.id]
    game["player"].append(game["deck"].pop())
    val = _hand_value(game["player"])
    cards = " ".join(_card_str(c) for c in game["player"])
    if val > BJ_BUST:
        await ctx.send(f"🃏 {cards} = **{val}** — **BUST!** Lost **{game['bet']}** coins.")
        del _bj_games[ctx.author.id]
    elif val == BJ_BUST:
        await ctx.send(f"🃏 {cards} = **21!** Use `!stand`.")
    else:
        await ctx.send(f"🃏 {cards} ({val}) — `!hit` or `!stand`")

@bot.command(name="stand")
async def stand_cmd(ctx):
    """Stand in blackjack, dealer draws."""
    if ctx.author.id not in _bj_games:
        await ctx.send("❌ No active blackjack game. `!bj <bet>` to start.")
        return
    game = _bj_games.pop(ctx.author.id)
    pval = _hand_value(game["player"])
    dhand = game["dealer"]
    while _hand_value(dhand) < 17:
        dhand.append(game["deck"].pop())
    dval = _hand_value(dhand)
    pcards = " ".join(_card_str(c) for c in game["player"])
    dcards = " ".join(_card_str(c) for c in dhand)
    if dval > BJ_BUST:
        payout = game["bet"] * 2
        add_coins(game["guild"], ctx.author.id, payout)
        await ctx.send(f"🃏 You: {pcards} ({pval})\n🂠 Dealer: {dcards} ({dval}) — **BUST!** You win **{payout}**!")
    elif dval > pval:
        await ctx.send(f"🃏 You: {pcards} ({pval})\n🂠 Dealer: {dcards} ({dval}) — Dealer wins. Lost **{game['bet']}**.")
    elif dval < pval:
        payout = game["bet"] * 2
        add_coins(game["guild"], ctx.author.id, payout)
        await ctx.send(f"🃏 You: {pcards} ({pval})\n🂠 Dealer: {dcards} ({dval}) — You win **{payout}**!")
    else:
        add_coins(game["guild"], ctx.author.id, game["bet"])
        await ctx.send(f"🃏 You: {pcards} ({pval})\n🂠 Dealer: {dcards} ({dval}) — **Push!** Bet returned.")

@bot.command(name="roulette")
@commands.cooldown(1, 3, commands.BucketType.user)
async def roulette_cmd(ctx, bet: int = None):
    """Russian roulette. 1 in 6 win, 5x payout. !roulette <bet>"""
    if bet is None:
        await ctx.send("Usage: `!roulette <bet>` — 1 in 6 chance. Win = 5x, lose = dead.")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, bet):
        await ctx.send(f"❌ You don't have **{bet}** coins.")
        return
    if random.randint(1, 6) == 1:
        payout = bet * 5
        add_coins(ctx.guild.id, ctx.author.id, payout)
        await ctx.send(f"🔫 *click* ... **BANG!** 💥 You survived and won **{payout}** coins!")
    else:
        await ctx.send(f"🔫 *click* ... Phew! But no prize. Lost **{bet}** coins.")

@bot.command(name="hilo", aliases=["hl"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def hilo_cmd(ctx, bet: int = None):
    """Higher or lower card game. !hilo <bet>"""
    if bet is None:
        await ctx.send("Usage: `!hilo <bet>` — guess `!higher` or `!lower`, `!cashout` to stop.\nStreak: 2x → 4x → 8x → 16x → 32x")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    if ctx.author.id in _hilo_games:
        await ctx.send("❌ You already have a hilo game. Use `!higher`, `!lower`, or `!cashout`.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, bet):
        await ctx.send(f"❌ You don't have **{bet}** coins.")
        return
    deck = _new_deck()
    card = deck.pop()
    _hilo_games[ctx.author.id] = {"bet": bet, "guild": ctx.guild.id, "card": card[0], "streak": 0, "deck": deck}
    await ctx.send(f"🂡 **{_card_str(card)}** — `!higher` or `!lower` — `!cashout` to take **{bet}** coins back")

@bot.command(name="higher")
async def higher_cmd(ctx):
    """Guess next card is higher in hilo."""
    await _hilo_guess(ctx, "higher")

@bot.command(name="lower")
async def lower_cmd(ctx):
    """Guess next card is lower in hilo."""
    await _hilo_guess(ctx, "lower")

@bot.command(name="cashout")
async def cashout_cmd(ctx):
    """Cash out current hilo winnings."""
    if ctx.author.id not in _hilo_games:
        await ctx.send("❌ No active hilo game.")
        return
    game = _hilo_games.pop(ctx.author.id)
    payouts = [0, 2, 4, 8, 16, 32]
    mult = payouts[min(game["streak"], len(payouts) - 1)]
    payout = game["bet"] * mult if mult else game["bet"]
    add_coins(game["guild"], ctx.author.id, payout)
    await ctx.send(f"Cashed out **{payout}** coins! ({mult}x)")

async def _hilo_guess(ctx, direction):
    if ctx.author.id not in _hilo_games:
        await ctx.send("❌ No active hilo game. `!hilo <bet>` to start.")
        return
    game = _hilo_games[ctx.author.id]
    old_val = game["card"]
    new_card = game["deck"].pop()
    new_val = new_card[0]
    correct = (direction == "higher" and new_val > old_val) or (direction == "lower" and new_val < old_val)
    if new_val == old_val:
        correct = False  # ties lose
    if correct:
        game["streak"] += 1
        game["card"] = new_val
        payouts = [0, 2, 4, 8, 16, 32]
        mult = payouts[min(game["streak"], len(payouts) - 1)]
        await ctx.send(f"🂡 **{_card_str(new_card)}** — Correct! Streak: **{game['streak']}** ({mult}x). `!higher` `!lower` `!cashout`")
    else:
        del _hilo_games[ctx.author.id]
        await ctx.send(f"🂡 **{_card_str(new_card)}** — Wrong! Lost **{game['bet']}** coins.")

@bot.command(name="race")
@commands.cooldown(1, 5, commands.BucketType.user)
async def race_cmd(ctx, bet: int = None, animal: str = None):
    """Bet on an animal race. !race <bet> [🐎🐕🐈🐓]"""
    if bet is None:
        await ctx.send("Usage: `!race <bet> [🐎🐕🐈🐓]` — pick an animal or random.\n🐎 2x 🐕 3x 🐈 4x 🐓 8x")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    if not remove_coins(ctx.guild.id, ctx.author.id, bet):
        await ctx.send(f"❌ You don't have **{bet}** coins.")
        return
    if animal and animal not in RACE_ANIMALS:
        animal = None
    if animal is None:
        animal = random.choice(list(RACE_ANIMALS))
    animals, weights = zip(*[(e, w) for e, (w, _) in RACE_ANIMALS.items()])
    winner = random.choices(animals, weights=weights, k=1)[0]
    track = " ".join(a for a in animals)
    if winner == animal:
        payout = bet * RACE_ANIMALS[animal][1]
        add_coins(ctx.guild.id, ctx.author.id, payout)
        await ctx.send(f"🏁 {track}\n🏆 **{winner}** wins! You picked **{animal}** — won **{payout}** coins!")
    else:
        await ctx.send(f"🏁 {track}\n🏆 **{winner}** wins! You picked **{animal}** — lost **{bet}** coins.")

@bot.command(name="rich", aliases=["baltop"])
async def rich_cmd(ctx):
    """Coin leaderboard."""
    eco = _load_economy()
    gid = str(ctx.guild.id)
    users = []
    for uid, data in eco.get(gid, {}).items():
        users.append((uid, data.get("coins", 0)))
    users.sort(key=lambda x: x[1], reverse=True)
    lines = ["**💰 Richest**", ""]
    for i, (uid, coins) in enumerate(users[:10], 1):
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else uid
        lines.append(f"{i}. **{name}** — {coins} coins")
    if not users:
        lines.append("No one has coins yet!")
    await ctx.send("\n".join(lines))

@bot.command(name="rob")
@commands.cooldown(1, 300, commands.BucketType.user)
async def rob_cmd(ctx, target: discord.Member = None):
    """Attempt to rob someone. !rob @user"""
    if target is None:
        await ctx.send("Usage: `!rob @user` — 40% success, steal 5-25% of their coins. 60% chance you pay 200 fine.")
        return
    if target == ctx.author:
        await ctx.send("❌ Can't rob yourself.")
        return
    if target.bot:
        await ctx.send("❌ Can't rob bots.")
        return
    target_coins = get_coins(ctx.guild.id, target.id)
    if target_coins < 100:
        await ctx.send(f"❌ {target.display_name} only has **{target_coins}** coins. Minimum 100 to rob.")
        return
    if has_active_item(ctx.guild.id, target.id, "padlock"):
        await ctx.send(f"🔒 {target.display_name} has a padlock! Rob failed.")
        return
    if random.random() < ROB_SUCCESS:
        stolen = int(target_coins * random.uniform(ROB_PCT_MIN, ROB_PCT_MAX))
        remove_coins(ctx.guild.id, target.id, stolen)
        add_coins(ctx.guild.id, ctx.author.id, stolen)
        await ctx.send(f"🦹 **{ctx.author.display_name}** robbed **{target.display_name}** for **{stolen}** coins!")
    else:
        fine = min(ROB_FINE, get_coins(ctx.guild.id, ctx.author.id))
        if fine:
            remove_coins(ctx.guild.id, ctx.author.id, fine)
            add_coins(ctx.guild.id, target.id, fine)
        await ctx.send(f"🚨 **{ctx.author.display_name}** got caught robbing **{target.display_name}**! Paid **{fine}** coin fine.")

@bot.command(name="work")
@commands.cooldown(1, 600, commands.BucketType.user)
async def work_cmd(ctx):
    """Earn coins with a random job. 10m cooldown."""
    jobs = [
        ("freelanced some code", (20, 80)),
        ("swept the server", (10, 40)),
        ("delivered pizza", (30, 100)),
        ("busked in voice chat", (15, 70)),
        ("filed TPS reports", (25, 90)),
        ("fixed a pipe", (35, 120)),
        ("packed orders at the warehouse", (20, 85)),
        ("returned shopping carts", (10, 50)),
        ("seized some crypto", (40, 150)),
        ("solved a CAPTCHA for a robot", (15, 60)),
    ]
    job, (lo, hi) = random.choice(jobs)
    coins = random.randint(lo, hi)
    add_coins(ctx.guild.id, ctx.author.id, coins)
    await ctx.send(f"Worked as {job} — earned **{coins}** coins!")

@bot.command(name="duel")
@commands.cooldown(1, 10, commands.BucketType.user)
async def duel_cmd(ctx, target: discord.Member = None, bet: int = None):
    """Challenge someone to a dice duel. !duel @user <bet>"""
    if not target or bet is None:
        await ctx.send("Usage: `!duel @user <bet>` — both roll d100, highest wins!")
        return
    if target == ctx.author or target.bot:
        await ctx.send("❌ Pick a real opponent.")
        return
    if bet < 1:
        await ctx.send("❌ Bet must be positive.")
        return
    uid1, uid2 = ctx.author.id, target.id
    if not remove_coins(ctx.guild.id, uid1, bet) or not remove_coins(ctx.guild.id, uid2, bet):
        await ctx.send("❌ Both players need enough coins!")
        return
    r1, r2 = random.randint(1, 100), random.randint(1, 100)
    if r1 > r2:
        add_coins(ctx.guild.id, uid1, bet * 2)
        result = f"🏆 **{ctx.author.display_name}** wins **{bet * 2}** coins!"
    elif r2 > r1:
        add_coins(ctx.guild.id, uid2, bet * 2)
        result = f"🏆 **{target.display_name}** wins **{bet * 2}** coins!"
    else:
        add_coins(ctx.guild.id, uid1, bet)
        add_coins(ctx.guild.id, uid2, bet)
        result = "🤝 Tie! Bots returned."
    await ctx.send(f"⚔️ {ctx.author.display_name} 🎲**{r1}** vs 🎲**{r2}** {target.display_name}\n{result}")

@bot.command(name="buyticket")
async def buyticket_cmd(ctx, amount: int = 1):
    """Buy lottery tickets. !buyticket [amount]"""
    if amount < 1:
        amount = 1
    cost = amount * 50
    if not remove_coins(ctx.guild.id, ctx.author.id, cost):
        await ctx.send(f"❌ You need **{cost}** coins for {amount} ticket(s).")
        return
    gid = str(ctx.guild.id)
    lotto = _lotteries.setdefault(gid, {"tickets": {}, "pool": 0, "last_draw": ""})
    lotto["tickets"][str(ctx.author.id)] = lotto["tickets"].get(str(ctx.author.id), 0) + amount
    lotto["pool"] += cost
    total = lotto["tickets"][str(ctx.author.id)]
    await ctx.send(f"🎟️ **{ctx.author.display_name}** bought {amount} ticket(s). You have **{total}**. Pool: **{lotto['pool']}** coins!")

@bot.command(name="lottery")
async def lottery_cmd(ctx):
    """View lottery stats."""
    gid = str(ctx.guild.id)
    lotto = _lotteries.get(gid)
    if not lotto or not lotto["tickets"]:
        await ctx.send("🎟️ No tickets sold yet. `!buyticket [amount]` to play!")
        return
    lines = [f"**🎟️ Lottery** — Pool: **{lotto['pool']}** coins", ""]
    uid_counts = list(lotto["tickets"].items())
    uid_counts.sort(key=lambda x: x[1], reverse=True)
    for uid, count in uid_counts[:10]:
        member = ctx.guild.get_member(int(uid))
        name = member.display_name if member else uid
        lines.append(f"**{name}** — {count} ticket(s)")
    lines.append("")
    lines.append("`!buyticket [amount]` — 50 coins each. Draws daily!")
    await ctx.send("\n".join(lines))

# --- RADIO ---
RADIO_CHANNEL_ID = 1530138658424881182
PRESETS = {
    # Australia
    "triplej": "triple j",
    "abc": "ABC Radio Sydney",
    "2gb": "2GB 873",
    "fox": "FOX FM Melbourne",
    "kiis": "KIIS 1065",
    "smooth": "smoothfm",
    "nova": "Nova 96.9",
    "triplem": "Triple M",
    # UK
    "bbc1": "BBC Radio 1",
    "bbc2": "BBC Radio 2",
    "bbc4": "BBC Radio 4",
    "bbc6": "BBC Radio 6 Music",
    "capital": "Capital FM",
    "heart": "Heart FM",
    "radiox": "Radio X",
    "absolute": "Absolute Radio",
    # US
    "npr": "NPR",
    "kiisfm": "KIIS FM Los Angeles",
    "z100": "Z100 New York",
    "kissfm": "KISS FM",
    "hot97": "Hot 97",
    # NZ
    "rnz": "RNZ National",
    "zb": "Newstalk ZB",
    "edge": "The Edge",
    # Canada
    "cbc": "CBC Radio One",
    # Japan
    "jwave": "J-Wave",
    "nhk": "NHK",
    # Philippines (direct stream URLs — not on radio-browser.info)
    "wish": "Wish 107.5",
    "monster": "Monster RX 93.1",
    "magic": "Magic 89.9",
    "barangay": "Barangay LS 97.1",
    "loveradio": "Love Radio",
    "yesfm": "Yes FM",
    "energyfm": "Energy FM",
    "dzrh": "DZRH",
    # Misc
    "europafm": "Europa FM",
    "fip": "FIP",
    "classicfm": "Classic FM",
    "jazz": "Jazz FM",
    "rock": "Rock FM",
    "bbcworld": "BBC World Service",
}
# Hardcoded stream URLs for stations not found on radio-browser.info
DIRECT_STREAMS = {
    "wish": "https://wishfmradio.purple.tools:2050/;",
    "monster": "https://sg-icecast.eradioportal.com:8443/rx931",
    "magic": "https://stream.zeno.fm/8g1q5z8y4x8uv",
    "barangay": "https://stream-152.zeno.fm/c5b1g3q8m98uv",
    "loveradio": "https://loveradiony.radioca.st/;",
    "yesfm": "https://stream-148.zeno.fm/6q4x6n8y4x8uv",
    "energyfm": "https://stream-152.zeno.fm/7q8x6n8y4x8uv",
    "dzrh": "https://stream-150.zeno.fm/1q8x6n8y4x8uv",
}
_radio_cache = {}  # {query: (timestamp, stations_list)}
_radio_now_playing = {}  # {guild_id: {"name": str, "url": str, "query": str, "started": float, "volume_pct": int}}
_iptv_stations = {}  # {lowercase_name: display_name} — loaded from iptv-org/database
_lotteries = {}  # {guild_id: {"tickets": {user_id: count}, "pool": int, "last_draw": str}}
_afk_users = {}  # {user_id: {"reason": str, "since": float}}
_reminders = []  # [{"user_id": int, "channel_id": int, "message": str, "trigger": float}]

CRYPTO = {
    "DOGE": {"name": "Dogecoin", "emoji": "🐕", "price": 0.12, "drift": 0.00008, "vol": 0.06, "prev": 0, "history": []},
    "BTC": {"name": "Bitcoin", "emoji": "₿", "price": 42000, "drift": 0.00005, "vol": 0.015, "prev": 0, "history": []},
    "ETH": {"name": "Ethereum", "emoji": "💎", "price": 2800, "drift": 0.00006, "vol": 0.025, "prev": 0, "history": []},
    "SOL": {"name": "Solana", "emoji": "☀️", "price": 140, "drift": 0.0001, "vol": 0.045, "prev": 0, "history": []},
    "PEPE": {"name": "PepeCoin", "emoji": "🐸", "price": 0.00008, "drift": 0.0000, "vol": 0.15, "prev": 0, "history": []},
    "NITRO": {"name": "NitroToken", "emoji": "⚡", "price": 10, "drift": 0.00015, "vol": 0.08, "prev": 0, "history": []},
}
# Pre-seed with tiny random walks so charts don't look dead after restart
for _sym, _coin in CRYPTO.items():
    base = _coin["price"]
    _coin["history"] = [base * (1 + random.uniform(-0.002, 0.002)) for _ in range(10)]
HISTORY_LEN = 120  # 2 hours of 1-min candles

def _narrate(symbol):
    """Generate a news-style market commentary for a coin."""
    coin = CRYPTO[symbol]
    h = coin["history"]
    name = coin["name"]
    price = coin["price"]
    prev = coin["prev"]
    vol = coin["vol"]

    if len(h) < 5:
        return "📡 Not enough data yet — market just opened."

    # 1h change
    ch_1h = None
    if len(h) >= 60:
        ch_1h = (price / h[-60] - 1) * 100

    # 5-min change
    ch_5m = (price / h[-5] - 1) * 100 if len(h) >= 5 else 0

    # detect spikes: any tick in last 10 with >3x normal vol move
    recent_returns = [(h[i] / h[i-1] - 1) for i in range(max(0, len(h)-10), len(h)) if i > 0]
    has_spike = any(abs(r) > vol * 3 for r in recent_returns) if recent_returns else False
    spike_dir = 1 if any(r > vol * 3 for r in recent_returns) else -1 if any(r < -vol * 3 for r in recent_returns) else 0

    # direction adjectives
    if ch_5m > 5:
        dir_word = "🚀 **SURGING**"
    elif ch_5m > 2:
        dir_word = "📈 Rallying"
    elif ch_5m > 0.5:
        dir_word = "↗️ Trending up"
    elif ch_5m > -0.5:
        dir_word = "↔️ Flat / consolidating"
    elif ch_5m > -2:
        dir_word = "↘️ Slipping"
    elif ch_5m > -5:
        dir_word = "📉 Selling off"
    else:
        dir_word = "💀 **CRASHING**"

    # volatility narrative
    recent_vol = sum(abs(r) for r in recent_returns) / max(len(recent_returns), 1)
    if recent_vol > vol * 2:
        vol_word = "Extremely volatile"
    elif recent_vol > vol:
        vol_word = "Choppy"
    elif recent_vol < vol * 0.3:
        vol_word = "Dead calm"
    else:
        vol_word = "Normal activity"

    sentences = [f"{dir_word} — {vol_word.lower()}."]

    if ch_1h is not None:
        sentences.append(f"1h: **{ch_1h:+.1f}%**.")
    if ch_5m:
        sentences.append(f"5m: **{ch_5m:+.1f}%**.")

    if has_spike:
        if spike_dir > 0:
            sentences.append(random.choice([
                "⚡ Sharp spike detected — possible whale buy or exchange listing.",
                "🐋 Whale alert! Big green candle just hit.",
                "📢 Sudden pump — news catalyst?",
            ]))
        else:
            sentences.append(random.choice([
                "🩸 Sharp dump — whale sell or FUD event.",
                "🧻 Weak hands folding. Someone big just exited.",
                "📉 Flash crash — leverage liquidations likely.",
            ]))

    # long-term narrative
    if ch_1h is not None and ch_1h > 10:
        sentences.append(random.choice(["Momentum is undeniable right now.", "This thing has legs.", "Bullish sentiment through the roof."]))
    elif ch_1h is not None and ch_1h < -10:
        sentences.append(random.choice(["Bloodbath. Absolute carnage.", "Bears in full control.", "Panic selling across the board."]))

    return " ".join(sentences)
RADIO_CACHE_TTL = 300

def _volume_pct_to_db(pct):
    """Convert volume percentage to dB gain for ffmpeg. 100% = 0dB, 1000% = +20dB."""
    import math
    if pct <= 0:
        return -60  # effectively mute
    return 20 * math.log10(pct / 100)

async def _start_radio_stream(vc, stream_url, guild_id, station_name, freq_or_preset, volume_pct=100, fry=0, crush=0, bass=0):
    """Start or restart a radio stream with volume + effects."""
    import math
    db_gain = 20 * math.log10(max(volume_pct, 1) / 100)
    filters = [f"volume={db_gain:.1f}dB"]
    # Bass boost: shelving filter at 80Hz
    if bass > 0:
        filters.append(f"bass=g={bass}:f=80:w=0.8")
    # Deepfry: crush dynamics → volume nuke → let PCM clipping destroy it
    if fry > 0:
        gain = min(fry * 3, 80)
        vol_boost = 1 + fry / 5  # fry=500 → 101x volume
        filters.append(f"compand=attacks=0:decays=0:points=-80/-80|-30/-12|-3/-1.5|0/-0.5:gain={gain},volume={vol_boost}")
    # Bitcrush: reduce bits + sample rate
    if crush > 0:
        bits = max(1, round(24 * (1 - crush / 100)))
        rate = max(2000, int(48000 * (1 - crush / 100 * 0.9)))
        filters.append(f"acrusher=bits={bits}:mode=log:aa=0")
        filters.append(f"aresample={rate}:osf=s16")
    ffmpeg_opts = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": f"-vn -af {','.join(filters)}",
    }
    if vc.is_playing():
        vc.stop()
    source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts)
    vc.play(source)
    _radio_now_playing[guild_id] = {
        "name": station_name, "url": stream_url,
        "query": freq_or_preset, "started": time.time(),
        "volume_pct": volume_pct, "fry": fry, "crush": crush, "bass": bass,
    }

def _load_iptv_database():
    """Load iptv-org/database radio stations into _iptv_stations. Downloads + caches channels.csv."""
    global _iptv_stations
    cache_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iptv_radio_cache.json")
    # Use cached copy if < 7 days old
    try:
        mtime = os.path.getmtime(cache_file)
        if time.time() - mtime < 604800:  # 7 days
            with open(cache_file, "r") as f:
                _iptv_stations = json.load(f)
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        resp = requests.get(
            "https://raw.githubusercontent.com/iptv-org/database/master/data/channels.csv",
            timeout=30
        )
        resp.raise_for_status()
        stations = {}
        for line in resp.text.split("\n"):
            parts = line.split(",")
            if len(parts) < 7:
                continue
            name, categories = parts[1], parts[6].lower()
            stations[name.strip().lower()] = name.strip()
        _iptv_stations = stations
        # Merge into PRESETS with auto-generated keys (first 20 chars, alphanumeric lowercase)
        for name_lower, name in stations.items():
            key = "".join(c for c in name_lower[:20] if c.isalnum())
            if key and key not in PRESETS:
                PRESETS[key] = name
        with open(cache_file, "w") as f:
            json.dump(stations, f)
        print(f"[radio] iptv-org database: {len(stations)} radio stations indexed")
    except Exception as e:
        print(f"[radio] iptv-org database load failed: {e}")
        # Try stale cache as fallback
        try:
            with open(cache_file, "r") as f:
                _iptv_stations = json.load(f)
        except Exception:
            pass

def _search_iptv_stations(query):
    """Search iptv-org database for matching station names."""
    query_lower = query.lower().strip()
    results = []
    for name_lower, name in _iptv_stations.items():
        if query_lower in name_lower:
            results.append(name)
            if len(results) >= 10:
                break
    return results

async def _search_stations(query):
    now = time.time()
    if query in _radio_cache and now - _radio_cache[query][0] < RADIO_CACHE_TTL:
        return _radio_cache[query][1]
    params = "name=" + requests.utils.quote(query) + "&limit=5&order=clickcount&reverse=true"
    url = f"https://de1.api.radio-browser.info/json/stations/search?{params}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                stations = await resp.json()
    except Exception:
        return []
    _radio_cache[query] = (now, stations)
    # Trim cache if it gets too big
    if len(_radio_cache) > 50:
        oldest = min(_radio_cache, key=lambda k: _radio_cache[k][0])
        del _radio_cache[oldest]
    return stations

@bot.command(name="tune")
@commands.cooldown(1, 5, commands.BucketType.guild)
async def tune_cmd(ctx, *, freq_or_preset: str = None):
    """Tune to an Australian radio station. !tune <preset/freq/name>"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    if not freq_or_preset:
        presets_list = ", ".join(f"`{k}`" for k in PRESETS)
        await ctx.send(f"Usage: `!tune <preset/frequency/station>`\nPresets: {presets_list}\n`!stop` to disconnect")
        return
    if not ctx.author.voice:
        await ctx.send("🔇 You need to be in a voice channel!")
        return

    query = PRESETS.get(freq_or_preset.lower().strip(), freq_or_preset.strip())
    preset_key = freq_or_preset.lower().strip()

    # Try direct stream URL first (for Philippine stations not on radio-browser)
    stream_url = None
    station_name = query
    if preset_key in DIRECT_STREAMS:
        stream_url = DIRECT_STREAMS[preset_key]

    if not stream_url:
        stations = await _search_stations(query)
        if not stations:
            # Fallback: try iptv-org database for station name matches, retry radio-browser
            iptv_matches = _search_iptv_stations(query)
            for alt_name in iptv_matches:
                stations = await _search_stations(alt_name)
                if stations:
                    break
        if not stations:
            await ctx.send(f"No stations found for: `{freq_or_preset}`")
            return
        station = stations[0]
        stream_url = station["url_resolved"]
        station_name = station["name"]

    voice_channel = ctx.author.voice.channel
    vc = ctx.voice_client
    if vc:
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)
    else:
        try:
            vc = await voice_channel.connect(reconnect=False)
        except (discord.ClientException, discord.errors.ConnectionClosed, TimeoutError) as e:
            await ctx.send(f"❌ Voice connect failed — Discord rejected the route (4017). Try again later.\n`{e}`")
            return

    await _start_radio_stream(vc, stream_url, ctx.guild.id, station_name, freq_or_preset, volume_pct=100)
    await ctx.send(f"📻 **{station_name}** (`{freq_or_preset}`)")

@bot.command(name="stop")
async def stop_cmd(ctx):
    """Stop radio and disconnect."""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        return
    vc = ctx.voice_client
    if vc and vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        _radio_now_playing.pop(ctx.guild.id, None)
        await ctx.send("📻 Disconnected.")
    else:
        await ctx.send("Not connected.")

@bot.command(name="stations", aliases=["presets"])
async def stations_cmd(ctx):
    """List built-in radio presets (+ iptv-org database count)."""
    lines = ["**Radio Presets**", ""]
    for key, query in PRESETS.items():
        lines.append(f"`!tune {key}` → {query}")
        if len(lines) > 60:  # Discord message limit safety
            lines.append(f"... and {len(PRESETS) - 55} more presets (iptv-org database)")
            break
    lines.append("")
    lines.append(f"Built-in + {len(_iptv_stations)} from iptv-org. Search: `!tune <any term>`")
    await ctx.send("\n".join(lines))

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx):
    """Show currently playing station."""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    if not np:
        await ctx.send("📻 Nothing playing. `!tune <station>` to start.")
        return
    elapsed = int(time.time() - np["started"])
    m, s = divmod(elapsed, 60)
    await ctx.send(f"📻 **{np['name']}** — playing for {m}m {s}s\n`!tune {np['query']}`")

@bot.command(name="volume", aliases=["vol"])
async def volume_cmd(ctx, vol: int = None):
    """Set radio volume with real dB gain. !volume <1-100000>"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    if vol is None or vol < 1:
        await ctx.send("Usage: `!volume <1-100000>` — 100% = 0dB, 1000% = +20dB, 100000% = +60dB")
        return
    vol = max(1, min(100000, vol))
    np = _radio_now_playing.get(ctx.guild.id)
    vc = ctx.voice_client
    if not np or not vc:
        await ctx.send("📻 Nothing playing. `!tune <station>` first.")
        return
    await _start_radio_stream(vc, np["url"], ctx.guild.id, np["name"], np["query"],
                              volume_pct=vol, fry=np.get("fry", 0), crush=np.get("crush", 0), bass=np.get("bass", 0))
    db_gain = _volume_pct_to_db(vol)
    label = f"**{vol}%** (+{db_gain:.0f}dB 🔥)" if vol > 100 else f"**{vol}%** ({db_gain:.0f}dB)"
    await ctx.send(f"Volume: {label}")

@bot.command(name="share")
async def share_cmd(ctx):
    """Share what's playing — posts an invite to the voice channel."""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    if not np or not ctx.author.voice:
        await ctx.send("📻 Nothing playing or you're not in voice.")
        return
    vc = ctx.author.voice.channel
    invite = await vc.create_invite(max_age=300, max_uses=10, reason="Radio share")
    await ctx.send(f"📻 **{np['name']}** — {ctx.author.mention} is listening!\nJoin: {invite.url}\n`!tune {np['query']}`")

@bot.command(name="deepfry")
async def deepfry_cmd(ctx, level: int = None):
    """Deepfry the radio audio — cascaded compression + clipping. !deepfry <1-500>"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    vc = ctx.voice_client
    if not np or not vc:
        await ctx.send("📻 Nothing playing. `!tune <station>` first.")
        return
    if level is None:
        await ctx.send(f"Usage: `!deepfry <1-500>` — current: **{np.get('fry', 0)}x**\n`!deepfry 0` to disable")
        return
    level = max(0, min(500, level))
    await _start_radio_stream(vc, np["url"], ctx.guild.id, np["name"], np["query"],
                              volume_pct=np.get("volume_pct", 100), fry=level, crush=np.get("crush", 0), bass=np.get("bass", 0))
    if level == 0:
        await ctx.send("Deepfry: off ")
    else:
        await ctx.send(f"Deepfry: **{level}x** 🔥 — +{min(level*3, 80)}dB compand, {1 + level / 5:.0f}x volume, hard clip")

@bot.command(name="bitcrush")
async def bitcrush_cmd(ctx, level: int = None):
    """Bitcrush the radio — reduce bit depth + sample rate. !bitcrush <1-100>"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    vc = ctx.voice_client
    if not np or not vc:
        await ctx.send("📻 Nothing playing. `!tune <station>` first.")
        return
    if level is None:
        bits = max(1, round(24 * (1 - np.get("crush", 0) / 100)))
        rate = max(2000, int(48000 * (1 - np.get("crush", 0) / 100 * 0.9)))
        await ctx.send(f"Usage: `!bitcrush <1-100>` — current: **{np.get('crush', 0)}%** ({bits}-bit / {rate}Hz)\n`!bitcrush 0` to disable")
        return
    level = max(0, min(100, level))
    await _start_radio_stream(vc, np["url"], ctx.guild.id, np["name"], np["query"],
                              volume_pct=np.get("volume_pct", 100), fry=np.get("fry", 0), crush=level, bass=np.get("bass", 0))
    if level == 0:
        await ctx.send("Bitcrush: off ")
    else:
        bits = max(1, round(24 * (1 - level / 100)))
        rate = max(2000, int(48000 * (1 - level / 100 * 0.9)))
        await ctx.send(f"Bitcrush: **{level}%** — {bits}-bit / {rate}Hz 🔧")

@bot.command(name="bassboost", aliases=["bass"])
async def bassboost_cmd(ctx, db: int = None):
    """Boost bass frequencies. !bassboost <+dB> — up to +100dB"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    vc = ctx.voice_client
    if not np or not vc:
        await ctx.send("📻 Nothing playing. `!tune <station>` first.")
        return
    if db is None:
        await ctx.send(f"Usage: `!bassboost <+dB>` — current: **+{np.get('bass', 0)}dB**\n`!bassboost 0` to disable")
        return
    db = max(0, min(100, db))
    await _start_radio_stream(vc, np["url"], ctx.guild.id, np["name"], np["query"],
                              volume_pct=np.get("volume_pct", 100), fry=np.get("fry", 0),
                              crush=np.get("crush", 0), bass=db)
    if db == 0:
        await ctx.send("Bass boost: off ")
    else:
        await ctx.send(f"Bass boost: **+{db}dB** 🎛️ — 80Hz shelf, rattle your windows")

@bot.command(name="record")
async def record_cmd(ctx, seconds: int = 10):
    """Record a clip of the radio stream. !record [5-60]"""
    if ctx.channel.id != RADIO_CHANNEL_ID:
        await ctx.send(f"📻 Use this in <#{RADIO_CHANNEL_ID}>")
        return
    np = _radio_now_playing.get(ctx.guild.id)
    if not np:
        await ctx.send("📻 Nothing playing. `!tune <station>` first.")
        return
    seconds = max(5, min(60, seconds))
    filename = f"/tmp/radio_clip_{ctx.guild.id}.mp3"
    msg = await ctx.send(f"⏺️ Recording {seconds}s from **{np['name']}**...")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", np["url"], "-t", str(seconds), "-vn",
        "-acodec", "libmp3lame", "-ar", "22050", "-ab", "64k", filename,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        await msg.edit(content=f"⏺️ **{np['name']}** — {seconds}s clip:")
        await ctx.send(file=discord.File(filename, f"radio_{seconds}s.mp3"))
        os.remove(filename)
    else:
        await msg.edit(content="❌ Recording failed. Stream may be down.")

@bot.command()
async def ask(ctx, *, user_input: str = None):
    if not user_input:
        await ctx.send("usage: !ask <question>")
        return
    attachments = ctx.message.attachments if ctx.message.attachments else None
    async with ctx.typing():
        response_text = await get_llm_response(
            ctx.channel.id, user_input, ctx.author.display_name, attachments
        )
        await send_split_response(ctx, response_text)

@bot.command()
async def prompt(ctx, *, new_prompt: str = ""):
    if not new_prompt:
        await ctx.send(f"Usage: `!prompt <system prompt>` — set the bot's personality/rules for this channel.\nCurrent: `{system_prompts.get(ctx.channel.id, DEFAULT_SYSTEM_PROMPT)[:100]}...`")
        return
    if len(new_prompt) > MAX_PROMPT_LENGTH:
        await ctx.send(f"prompt too long ({len(new_prompt)} chars). max is {MAX_PROMPT_LENGTH}.")
        return
    system_prompts[ctx.channel.id] = new_prompt
    chat_history[ctx.channel.id] = [{"role": "system", "content": new_prompt}]
    await ctx.send("kitchen rules updated.")

@bot.command()
async def clear(ctx):
    sys_msg = system_prompts.get(ctx.channel.id, DEFAULT_SYSTEM_PROMPT)
    chat_history[ctx.channel.id] = [{"role": "system", "content": sys_msg}]
    await ctx.send("history cleared.")

@bot.command(name="auto-reply")
async def auto_reply_cmd(ctx, action: str = ""):
    if action.lower() == "enable":
        auto_reply_enabled[ctx.channel.id] = True
        await ctx.send("auto-reply on.")
    elif action.lower() == "disable":
        auto_reply_enabled[ctx.channel.id] = False
        await ctx.send("auto-reply off.")
    else:
        await ctx.send("usage: !auto-reply enable|disable")


# --- BACKGROUND TASKS ---

async def _lottery_loop():
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(3600)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for gid, lotto in list(_lotteries.items()):
                if not lotto["tickets"] or lotto.get("last_draw") == today:
                    continue
                tickets = [(uid, count) for uid, count in lotto["tickets"].items() for _ in range(count)]
                if not tickets:
                    continue
                winner_uid = random.choice(tickets)[0]
                pool = lotto["pool"]
                guild = bot.get_guild(int(gid))
                if guild:
                    member = guild.get_member(int(winner_uid))
                    winner_name = member.display_name if member else winner_uid
                    channel = guild.system_channel or guild.text_channels[0]
                    add_coins(gid, int(winner_uid), pool)
                    await channel.send(f"**{winner_name}** won the lottery! **{pool}** coins!")
                lotto["tickets"] = {}
                lotto["pool"] = 0
                lotto["last_draw"] = today
        except Exception as e:
            print(f"[lotto:err] {e}")

async def _reminder_loop():
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(30)
        try:
            now = time.time()
            for rem in list(_reminders):
                if rem["trigger"] <= now:
                    channel = bot.get_channel(rem["channel_id"])
                    if channel:
                        await channel.send(f"<@{rem['user_id']}> reminder: {rem['message']}")
                    _reminders.remove(rem)
        except Exception as e:
            print(f"[remind:err] {e}")

async def _crypto_loop():
    """Geometric Brownian motion with momentum + news events. Updates every 60s."""
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(60)
        try:
            for sym, coin in CRYPTO.items():
                old = coin["price"]
                # GBM: dS = μS·dt + σS·dW    →    S_new = S * exp((μ - σ²/2)·dt + σ·√dt·ε)
                dt = 1.0 / 525600  # 1 minute in years (reasonable scaling)
                drift = coin["drift"]
                vol = coin["vol"]
                epsilon = random.gauss(0, 1)
                # momentum: 30% weight on previous direction
                if coin["prev"] != 0:
                    epsilon = 0.7 * epsilon + 0.3 * (1 if coin["prev"] > 0 else -1)
                gbm = (drift - 0.5 * vol ** 2) * dt + vol * (dt ** 0.5) * epsilon
                coin["price"] *= (1 + gbm)  # discrete approximation — safe & close to exp(gbm)
                # news event: 2% chance of ±10-40% spike
                if random.random() < 0.02:
                    spike = random.uniform(0.10, 0.40) * random.choice([-1, 1])
                    coin["price"] *= (1 + spike)
                coin["price"] = round(max(0.00000001, coin["price"]), 8)
                coin["prev"] = coin["price"] - old
                coin["history"].append(coin["price"])
                if len(coin["history"]) > HISTORY_LEN:
                    coin["history"] = coin["history"][-HISTORY_LEN:]
        except Exception as e:
            print(f"[crypto:err] {e}")

def _get_crypto_portfolio(guild_id, user_id):
    eco = _load_economy()
    return eco.get(str(guild_id), {}).get(str(user_id), {}).get("crypto", {})

def _set_crypto_portfolio(guild_id, user_id, pf):
    eco = _load_economy()
    eco.setdefault(str(guild_id), {}).setdefault(str(user_id), {})["crypto"] = pf
    _save_economy(eco)


# --- CRYPTO COMMANDS ---

@bot.command(name="crypto", aliases=["market"])
async def crypto_cmd(ctx):
    """View crypto market prices."""
    lines = ["**📈 Crypto Market** (1-min ticks)", ""]
    for sym, coin in sorted(CRYPTO.items()):
        trend = ""
        if coin["prev"] > 0:
            trend = " 📈"
        elif coin["prev"] < 0:
            trend = " 📉"
        lines.append(f"{coin['emoji']} **{sym}** — {coin['price']:.8f}{trend}")
    lines.append("")
    lines.append("`!buycoin <sym> <coins>` | `!sellcoin <sym>` | `!portfolio` | `!chart <sym>`")
    await ctx.send("\n".join(lines))

@bot.command(name="chart")
async def chart_cmd(ctx, symbol: str = ""):
    """Show price chart + narration. !chart [sym]  (no arg=all)"""
    symbol = symbol.upper()
    if symbol:
        if symbol not in CRYPTO:
            syms = ", ".join(CRYPTO)
            await ctx.send(f"Usage: `!chart <symbol>` — {syms}")
            return
        coin = CRYPTO[symbol]
        h = coin["history"]
        if len(h) < 5:
            await ctx.send(f"{coin['emoji']} **{symbol}** — not enough data yet. Check back in a few minutes.")
            return
        current = coin["price"]
        low, high = min(h), max(h)
        recent = h[-min(20, len(h)):]
        rng = max(high - low, 0.00000001)
        bars = "▁▂▃▄▅▆▇█"
        spark = "".join(bars[min(int((p - low) / rng * 7), 7)] for p in recent)
        ch_1h = ((current / h[max(0, len(h) - 60)] - 1) * 100) if len(h) >= 60 else None
        lines = [
            f"{coin['emoji']} **{symbol}** ({coin['name']}) — **{current:.8f}**",
            f"Range: {low:.8f} — {high:.8f}" + (f" | 1h: **{ch_1h:+.2f}%**" if ch_1h is not None else ""),
            f"```{spark}```",
            _narrate(symbol),
        ]
        await ctx.send("\n".join(lines))
    else:
        # All coins overview
        lines = ["**📊 Market Overview**", ""]
        for sym, coin in sorted(CRYPTO.items()):
            h = coin["history"]
            if len(h) < 5:
                lines.append(f"{coin['emoji']} **{sym}** — loading…")
                continue
            recent = h[-min(20, len(h)):]
            low, high = min(h), max(h)
            rng = max(high - low, 0.00000001)
            bars = "▁▂▃▄▅▆▇█"
            spark = "".join(bars[min(int((p - low) / rng * 7), 7)] for p in recent)
            ch_5m = (coin["price"] / h[-min(5, len(h))] - 1) * 100
            arrow = "📈" if ch_5m > 0.2 else "📉" if ch_5m < -0.2 else "➡️"
            lines.append(f"{arrow} {coin['emoji']} **{sym}** `{spark}` {coin['price']:.8f} ({ch_5m:+.1f}%)")
        await ctx.send("\n".join(lines))

@bot.command(name="buycoin")
async def buycoin_cmd(ctx, symbol: str = "", amount: int = None):
    """Buy crypto. !buycoin <sym> <coin_amount>"""
    symbol = symbol.upper()
    if symbol not in CRYPTO or amount is None:
        await ctx.send("Usage: `!buycoin <symbol> <coin_amount>` — e.g. `!buycoin DOGE 100`")
        return
    if amount < 1:
        await ctx.send("❌ Minimum 1 coin.")
        return
    price = CRYPTO[symbol]["price"]
    cost = round(amount * price, 8)
    if not remove_coins(ctx.guild.id, ctx.author.id, cost):
        await ctx.send(f"❌ You need **{cost}** coins. You have **{get_coins(ctx.guild.id, ctx.author.id)}**.")
        return
    pf = _get_crypto_portfolio(ctx.guild.id, ctx.author.id)
    pf[symbol] = pf.get(symbol, 0) + amount
    _set_crypto_portfolio(ctx.guild.id, ctx.author.id, pf)
    await ctx.send(f"{CRYPTO[symbol]['emoji']} Bought **{amount} {symbol}** for **{cost}** coins!")

@bot.command(name="sellcoin")
async def sellcoin_cmd(ctx, symbol: str = ""):
    """Sell all of a crypto. !sellcoin <sym>"""
    symbol = symbol.upper()
    if symbol not in CRYPTO:
        await ctx.send("Usage: `!sellcoin <symbol>` — e.g. `!sellcoin DOGE`")
        return
    pf = _get_crypto_portfolio(ctx.guild.id, ctx.author.id)
    amount = pf.get(symbol, 0)
    if amount <= 0:
        await ctx.send(f"❌ You don't own any {symbol}.")
        return
    price = CRYPTO[symbol]["price"]
    total = round(amount * price, 8)
    del pf[symbol]
    _set_crypto_portfolio(ctx.guild.id, ctx.author.id, pf)
    add_coins(ctx.guild.id, ctx.author.id, total)
    await ctx.send(f"{CRYPTO[symbol]['emoji']} Sold **{amount} {symbol}** for **{total}** coins!")

@bot.command(name="portfolio", aliases=["pf"])
async def portfolio_cmd(ctx):
    """View your crypto portfolio."""
    pf = _get_crypto_portfolio(ctx.guild.id, ctx.author.id)
    if not pf:
        await ctx.send("📉 Empty portfolio. `!crypto` to see the market!")
        return
    lines = [f"**📊 {ctx.author.display_name}'s Portfolio**", ""]
    total = 0
    for sym, amount in sorted(pf.items()):
        coin = CRYPTO.get(sym, {"price": 0, "emoji": "❓"})
        val = round(amount * coin["price"], 8)
        total += val
        lines.append(f"{coin['emoji']} **{sym}** — {amount} (worth {val} coins)")
    lines.append(f"\n**Total value: {total:.2f}** coins")
    await ctx.send("\n".join(lines))


# --- BOARD GAMES ---

@bot.command(name="tictactoe", aliases=["ttt"])
async def tictactoe_cmd(ctx, opponent: discord.Member = None, bet: int = 0):
    """Play TicTacToe. !tictactoe @user [bet]"""
    if not opponent or opponent == ctx.author or opponent.bot:
        await ctx.send("Usage: `!tictactoe @user [bet]`")
        return
    if bet:
        if not remove_coins(ctx.guild.id, ctx.author.id, bet) or not remove_coins(ctx.guild.id, opponent.id, bet):
            await ctx.send("❌ Both players need enough coins!")
            return
    view = TicTacToeView(ctx.author, opponent, bet, ctx.guild.id)
    await ctx.send(view._names(), view=view)

@bot.command(name="connect4", aliases=["c4"])
async def connect4_cmd(ctx, opponent: discord.Member = None, bet: int = 0):
    """Play Connect 4. !connect4 @user [bet]"""
    if not opponent or opponent == ctx.author or opponent.bot:
        await ctx.send("Usage: `!connect4 @user [bet]`")
        return
    if bet:
        if not remove_coins(ctx.guild.id, ctx.author.id, bet) or not remove_coins(ctx.guild.id, opponent.id, bet):
            await ctx.send("❌ Both players need enough coins!")
            return
    view = Connect4View(ctx.author, opponent, bet, ctx.guild.id)
    await ctx.send(view._render(), view=view)


# --- UTILITY COMMANDS ---

@bot.command(name="poll")
async def poll_cmd(ctx, *, args: str = ""):
    """Create a reaction poll. !poll <question> | <opt1> | <opt2> ..."""
    if not args:
        await ctx.send("Usage: `!poll <question> | <option1> | <option2> ...`")
        return
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3:
        await ctx.send("Usage: `!poll <question> | <option1> | <option2> ...`")
        return
    question, options = parts[0], parts[1:]
    if len(options) > 9:
        options = options[:9]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    embed = discord.Embed(title=f"📊 {question}", color=0x3498db)
    embed_desc = []
    for i, opt in enumerate(options):
        embed_desc.append(f"{emojis[i]} {opt}")
    embed.description = "\n".join(embed_desc)
    embed.set_footer(text=f"Poll by {ctx.author.display_name}")
    msg = await ctx.send(embed=embed)
    try:
        for i in range(len(options)):
            await msg.add_reaction(emojis[i])
    except discord.Forbidden:
        await ctx.send("I need permission to add reactions!")

@bot.command(name="remind")
async def remind_cmd(ctx, time_str: str = "", *, message: str = ""):
    """Set a reminder. !remind <time> <message>"""
    if not time_str or not message:
        await ctx.send("Usage: `!remind <time> <message>` — e.g. `!remind 10m check dinner`")
        return
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        unit = time_str[-1].lower()
        val = int(time_str[:-1])
        seconds = val * mult.get(unit, 60)
        seconds = min(seconds, 86400 * 7)
    except (ValueError, IndexError):
        await ctx.send("❌ Invalid time. Try: `10m`, `1h`, `30s`, `2d`")
        return
    trigger = time.time() + seconds
    _reminders.append({"user_id": ctx.author.id, "channel_id": ctx.channel.id, "message": message, "trigger": trigger})
    await ctx.send(f"Reminder set for {time_str}.")

@bot.command(name="afk")
async def afk_cmd(ctx, *, reason: str = "AFK"):
    """Set AFK status. !afk [reason]"""
    _afk_users[ctx.author.id] = {"reason": reason, "since": time.time()}
    await ctx.send(f"**{ctx.author.display_name}** is now AFK: {reason}")


@bot.command(name="lang")
async def lang_cmd(ctx, *, lang: str = ""):
    """Set your language for auto-translate. !lang fr / !lang off / !lang (check)"""
    uid = str(ctx.author.id)
    langs = _load_langs()
    if not lang.strip():
        current = langs.get(uid)
        if current:
            await ctx.send(f"Your language is set to **{current}**. Use `!lang off` to disable.")
        else:
            await ctx.send("You don't have a language set. Use `!lang <code>` (e.g. `!lang fr`, `!lang ja`, `!lang tl`). The bot will DM you translations when someone speaks a different language.")
        return
    lang = lang.strip().lower()
    if lang in ("off", "none", "disable", "reset"):
        langs.pop(uid, None)
        _save_langs(langs)
        await ctx.send("Auto-translate disabled.")
    else:
        langs[uid] = lang
        _save_langs(langs)
        await ctx.send(f"Language set to **{lang}**. You'll get DMs when someone speaks a different language.")


# --- EVENTS ---

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        remaining = max(1, int(error.retry_after))
        if remaining >= 3600:
            t = f"{remaining // 3600}h {(remaining % 3600) // 60}m"
        elif remaining >= 60:
            t = f"{remaining // 60}m {remaining % 60}s"
        else:
            t = f"{remaining}s"
        await ctx.send(f"⏳ Cooldown. Try again in **{t}**.")
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"❌ Bad args. Try `!help` or just `!{ctx.command.name}` for usage.")
    else:
        print(f"[cmd:err] {ctx.command}: {error}")

@bot.event
async def on_ready():
    print(f"logged in as {bot.user.name}")
    # Pre-warm NLLB translation model (loads to RAM, avoids cold-start on first translate)
    asyncio.create_task(asyncio.to_thread(_init_nllb))
    # Load iptv-org radio station database in background
    asyncio.create_task(asyncio.to_thread(_load_iptv_database))
    asyncio.create_task(_lottery_loop())
    asyncio.create_task(_reminder_loop())
    asyncio.create_task(_crypto_loop())
    asyncio.create_task(_flush_loop())

async def _flush_loop():
    """Write dirty caches to disk every 10s (atomic, safe against power loss)."""
    global _economy_dirty, _levels_dirty
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(10)
        try:
            if _economy_dirty:
                _economy_dirty = False
                tmp = ECO_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(_economy_cache, f, indent=2)
                os.replace(tmp, ECO_FILE)
            if _levels_dirty:
                _levels_dirty = False
                tmp = LEVEL_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(_levels_cache, f, indent=2)
                os.replace(tmp, LEVEL_FILE)
        except Exception as e:
            print(f"[flush:err] {e}")

@bot.event
async def on_member_join(member):
    # Welcome DM — tell them about key commands
    try:
        welcome = (
            f"Welcome to **{member.guild.name}**, {member.mention}.\n\n"
            f"Set your language for auto-translate: `!lang tl` (Tagalog), `!lang ja` (Japanese), `!lang en` (English), etc.\n"
            f"  Use `!lang off` to disable.\n"
            f"Check your rank: `!rank`\n"
            f"Economy: `!daily` `!balance` `!work`\n"
            f"Full list: `!help`"
        )
        await member.send(welcome)
    except (discord.Forbidden, discord.HTTPException):
        pass  # DMs closed

    if not get_guild_config(member.guild.id, "auto_role_enabled"):
        return
    role = discord.utils.get(member.guild.roles, name="Member")
    if role:
        try:
            await member.add_roles(role)
            print(f"[auto-role] gave Member to {member.name}")
        except discord.Forbidden:
            print(f"[auto-role] no perms to give Member to {member.name}")
        except discord.HTTPException as e:
            print(f"[auto-role] HTTP error for {member.name}: {e}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # AFK auto-reply
    if message.guild and message.mentions:
        for mentioned in message.mentions:
            afk = _afk_users.get(mentioned.id)
            if afk and not _afk_users.get(message.author.id):  # don't spam if afk user sends message
                elapsed = int(time.time() - afk["since"])
                m, s = divmod(elapsed, 60)
                await message.channel.send(f"**{mentioned.display_name}** is AFK ({m}m {s}s ago): {afk['reason']}", delete_after=30)

    # Clear AFK when user sends a message
    if message.author.id in _afk_users:
        del _afk_users[message.author.id]
        await message.channel.send(f"Welcome back **{message.author.display_name}**.", delete_after=10)

    # Active item: Megaphone — pin the next message
    if message.guild and has_active_item(message.guild.id, message.author.id, "megaphone"):
        try:
            await message.pin()
            consume_item(message.guild.id, message.author.id, "megaphone")
            await message.channel.send(f"📢 Pinned {message.author.mention}'s message!", delete_after=10)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Active item: Spotlight — embed announcement
    if message.guild and has_active_item(message.guild.id, message.author.id, "spotlight"):
        consume_item(message.guild.id, message.author.id, "spotlight")
        embed = discord.Embed(description=message.content[:1000], color=0xf1c40f, timestamp=message.created_at)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        await message.channel.send(embed=embed)

    # word blacklist
    gid = message.guild.id if message.guild else 0
    if get_guild_config(gid, "blacklist_enabled"):
        hits = _check_blacklist(message)
    else:
        hits = set()
    if hits:
        try:
            await message.delete()
            print(f"[blacklist] deleted msg from {message.author.name}: {hits}")
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            roast = await get_llm_response(
                message.channel.id,
                f'Someone just said something bad and got deleted. They said: "{message.content[:200]}". Roast them with a short, {get_guild_config(message.guild.id, "roast_style")} one-liner. Max 150 chars. Name: {message.author.display_name}',
                "system",
                None
            )
            await message.channel.send(f"{message.author.mention} {roast}")
        except Exception:
            pass
        return

    await _check_ping_spam(message)

    # AI safety filter — runs after blacklist, before LLM reply
    if get_guild_config(message.guild.id if message.guild else 0, "safety_enabled"):
        async with _api_semaphore:
            if await _check_safety(message):
                try:
                    await message.delete()
                    print(f"[safety] ai-flagged msg from {message.author.name}")
                    roast = await get_llm_response(
                        message.channel.id,
                        f'Someone sent a message that got flagged as unsafe. They said: "{message.content[:200]}". Roast them with a short one-liner about why it crossed the line. Max 150 chars. Name: {message.author.display_name}',
                        "system",
                        None
                    )
                    await message.channel.send(f"{message.author.mention} {roast}")
                except Exception:
                    pass
                return

    # Auto-translate: background task, doesn't block the pipeline
    asyncio.create_task(_maybe_translate(message))

    await bot.process_commands(message)

    # Log EVERY message to history so the bot remembers all chat
    cid = message.channel.id
    if cid not in chat_history:
        sys_msg = system_prompts.get(cid, DEFAULT_SYSTEM_PROMPT)
        chat_history[cid] = [{"role": "system", "content": sys_msg}]
    chat_history[cid].append({"role": "user", "content": f"{message.author.display_name}: {message.content}"})
    if len(chat_history[cid]) > 50:
        chat_history[cid] = [chat_history[cid][0]] + chat_history[cid][-20:]

    if message.content.startswith(bot.command_prefix):
        return

    gid = message.guild.id if message.guild else message.channel.id
    chatbot_on = get_guild_config(gid, "chatbot_enabled")
    # XP gain + coin earning (cooldown: 10s)
    last_xp = _last_xp_time.get(message.author.id, 0)
    if message.guild and time.time() - last_xp >= XP_COOLDOWN:
        _last_xp_time[message.author.id] = time.time()
        xp_to_add = XP_PER_MESSAGE * 2 if has_active_item(message.guild.id, message.author.id, "xpboost") else XP_PER_MESSAGE
        leveled_up, new_level = add_xp(message.guild.id, message.author.id, xp_override=xp_to_add)
        if leveled_up:
            await message.channel.send(f"{message.author.mention} reached level **{new_level}**!")
        if get_guild_config(message.guild.id, "economy_enabled"):
            eco = _load_economy()
            gid, uid = str(message.guild.id), str(message.author.id)
            # per-message coins
            min_c = get_guild_config(message.guild.id, "coins_per_message_min")
            max_c = get_guild_config(message.guild.id, "coins_per_message_max")
            coins_gain = random.randint(min_c, max_c)
            if has_active_item(message.guild.id, message.author.id, "coinmagnet"):
                coins_gain *= 2
            eco.setdefault(gid, {}).setdefault(uid, {"coins": 0})
            eco[gid][uid]["coins"] += coins_gain
            # daily interest
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if eco[gid][uid].get("last_interest", "") != today:
                bal = eco[gid][uid].get("coins", 0)
                if bal >= 50:
                    interest = min(int(bal * INTEREST_RATE), INTEREST_CAP)
                    eco[gid][uid]["coins"] += interest
                eco[gid][uid]["last_interest"] = today
            _save_economy(eco)

    # Rate limit — only block LLM response, not moderation
    rate_limit = get_guild_config(message.guild.id if message.guild else 0, "rate_limit_seconds")
    last = _last_message_time.get(message.author.id, 0)
    if time.time() - last < rate_limit:
        _last_message_time[message.author.id] = time.time()
        should_respond = False
    else:
        _last_message_time[message.author.id] = time.time()
        should_respond = chatbot_on and (
            bot.user.mentioned_in(message)
            or auto_reply_enabled.get(cid, False)
            or isinstance(message.channel, discord.DMChannel)
    )

    if should_respond:
        clean_prompt = message.content.replace(f"<@{bot.user.id}>", "").strip()
        attachments = message.attachments if message.attachments else None
        if not clean_prompt and not attachments:
            return
        async with message.channel.typing():
            response_text = await get_llm_response(
                message.channel.id, clean_prompt, message.author.display_name, attachments
            )
            await send_split_response(message, response_text)

bot.run(TOKEN)
