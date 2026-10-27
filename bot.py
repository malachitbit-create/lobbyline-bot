#!/usr/bin/env python3
"""Lobby Line draft bot: news -> Gemini draft -> owner approval -> @lobbyline."""
import os, sys, json, time, re, html, hashlib, logging, threading, random, datetime as dt
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Yekaterinburg")
CHANNEL = "@lobbyline"
SCHEDULE = [(10, 0), (15, 0), (20, 0)]
MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest"]
DATA = os.environ.get("DATA_DIR") or BASE          # persistent runtime data (/data/storage on FadeHost)
ASSETS = os.path.join(BASE, "assets")               # read-only files shipped with the code
os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)
STATE = os.path.join(DATA, "state.json")
MEDIA_DIR = os.path.join(DATA, "media")
PHOTO_MAX, VIDEO_MAX = 10 * 2**20, 50 * 2**20
UA = {"User-Agent": "Mozilla/5.0 (LobbyLineBot)"}

logging.basicConfig(handlers=[logging.FileHandler(os.path.join(DATA, "logs", "bot.log")), logging.StreamHandler(sys.stdout)], level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger()

def load_env(path):
    if os.path.exists(path):
        for l in open(path):
            if "=" in l and not l.startswith("#"):
                k, v = l.strip().split("=", 1); os.environ.setdefault(k, v.strip().strip('"'))
load_env("/home/box/tg/bot.env"); load_env(os.path.join(BASE, "gemini.env"))  # local only; on the host secrets come from env vars
def _seed_state():
    """First start on a new host: restore state.json from the encrypted seed shipped with the code."""
    seed = os.path.join(BASE, "state_seed.enc")
    if not os.path.exists(seed) or not os.environ.get("STATE_KEY"): return
    sid = hashlib.md5(open(seed, "rb").read()).hexdigest(); mark = STATE + ".seed_id"
    if os.path.exists(mark) and open(mark).read().strip() == sid: return  # this seed was already applied once
    from cryptography.fernet import Fernet
    import gzip
    data = gzip.decompress(Fernet(os.environ["STATE_KEY"].encode()).decrypt(open(seed, "rb").read()))
    json.loads(data); open(STATE, "wb").write(data); open(mark, "w").write(sid); print("state.json restored from seed", sid, flush=True)
_seed_state()
TOKEN = os.environ["BOT_TOKEN"]; GKEY = os.environ["GEMINI_API_KEY"]
API = f"https://api.telegram.org/bot{TOKEN}/"

GAMES = {
  "Dota 2": ["dota"], "CS 2": ["counter-strike", "cs2", "cs 2", "кс2"], "Fortnite": ["fortnite", "фортнайт"],
  "Valorant": ["valorant", "валорант"], "World of Tanks": ["world of tanks", "wot", "танк"],
  "Hearthstone": ["hearthstone", "хартстоун"], "Overwatch": ["overwatch", "овервотч"],
  "The Finals": ["the finals"], "GTA 5": ["gta", "grand theft auto"]}
STEAM = {"Dota 2": 570, "CS 2": 730, "The Finals": 2073850, "GTA 5": 271590, "Overwatch": 2357570, "World of Tanks": 1407200}
RSS = ["https://rss.stopgame.ru/rss_news.xml", "https://www.igromania.ru/rss/news.rss",
       "https://www.playground.ru/rss/news.xml", "https://news.blizzard.com/ru-ru/rss",
       "https://www.fortnite.com/news/rss", "https://playvalorant.com/ru-ru/news/rss.xml"]

lock = threading.Lock()          # guards state.json read-modify-write; never hold it during network I/O
job_lock = threading.Lock()      # serializes heavy background jobs (news, memes)
JOB_LIMIT = 900                  # seconds per background job
HEARTBEAT = os.path.join(DATA, "logs", "heartbeat")
_orig_request = requests.sessions.Session.request
def _request_with_timeout(self, method, url, **kw):
    """Every HTTP call gets connect 10 s / read 30 s (Gemini 60 s) unless it sets more specific limits."""
    t = kw.get("timeout"); rd = 60 if "generativelanguage" in str(url) else 30
    if t is None: kw["timeout"] = (10, rd)
    elif isinstance(t, (int, float)): kw["timeout"] = (min(10, t), t)
    return _orig_request(self, method, url, **kw)
requests.sessions.Session.request = _request_with_timeout
def run_job(name, fn, *a, limit=JOB_LIMIT, **kw):
    """Run fn in its own daemon thread; log an error if it exceeds the limit (polling never waits on it)."""
    th = threading.Thread(target=lambda: _safe(name, fn, *a, **kw), daemon=True, name=name); th.start(); th.join(limit)
    if th.is_alive(): log.error("job %s exceeded %ss, abandoned", name, limit)
def _safe(name, fn, *a, **kw):
    try: fn(*a, **kw)
    except Exception: log.exception("job %s", name)
from concurrent.futures import ThreadPoolExecutor
POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="cb")
def watchdog():
    while True:
        time.sleep(30)
        try: age = time.time() - os.path.getmtime(HEARTBEAT)
        except OSError: continue
        if age > 300:
            log.error("watchdog: heartbeat is %ds old, polling hung; exiting for restart", age)
            logging.shutdown(); os._exit(1)
def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"owner": None, "used": [], "drafts": {}}
def save_state(s):
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, STATE)

# ---------- telegram ----------
def tg(method, files=None, **params):
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)): params[k] = json.dumps(v, ensure_ascii=False)
    to = (5, 10) if method.startswith("answer") else (10, 60) if files else (10, 30)
    try: r = requests.post(API + method, data=params, files=files, timeout=to)
    except requests.RequestException as e:
        log.warning("tg %s: %s", method, type(e).__name__); return {"ok": False, "description": type(e).__name__}
    j = r.json()
    if not j.get("ok"): log.warning("tg %s failed: %s", method, j.get("description"))
    return j

# ---------- news ----------
def match_game(text):
    t = text.lower()
    for g, keys in GAMES.items():
        if any(k in t for k in keys): return g

def strip_html(s): return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()

def fetch_steam():
    out = []
    for game, appid in STEAM.items():
        try:
            j = requests.get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                params={"appid": appid, "count": 4, "maxlength": 0, "format": "json"}, timeout=20, headers=UA).json()
            for it in j["appnews"]["newsitems"]:
                if time.time() - it["date"] > 4 * 86400: continue
                body = it.get("contents", "")
                imgs = re.findall(r'\[img\](.*?)\[/img\]', body) + re.findall(r'<img[^>]+src="([^"]+)"', body)
                imgs = [i.replace("{STEAM_CLAN_IMAGE}", "https://clan.akamai.steamstatic.com/images") for i in imgs]
                yt = re.findall(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+)', body)
                yt += ["https://youtu.be/" + v for v in re.findall(r'\[previewyoutube=([\w-]+)', body)]
                out.append({"id": "steam:" + it["gid"], "game": game, "title": it["title"], "url": it["url"],
                            "text": strip_html(re.sub(r"\[/?[^\]]+\]", " ", body))[:3000],
                            "images": imgs, "videos": [], "youtube": yt, "ts": it["date"]})
        except Exception as e: log.warning("steam %s: %s", game, e)
    return out

def _pubts(it):
    from email.utils import parsedate_to_datetime
    try: return parsedate_to_datetime(it.findtext("pubDate")).timestamp()
    except Exception: return time.time()
NS = {"media": "http://search.yahoo.com/mrss/"}
def fetch_rss():
    out = []
    for url in RSS:
        try:
            root = ET.fromstring(requests.get(url, timeout=20, headers=UA).content)
        except Exception as e: log.warning("rss %s: %s", url, e); continue
        for it in root.iter("item"):
            title = it.findtext("title") or ""; desc = it.findtext("description") or ""
            game = match_game(title + " " + desc)
            if not game: continue
            link = (it.findtext("link") or "").strip()
            imgs, vids = [], []
            for el in it.findall("enclosure") + it.findall("media:content", NS) + it.findall("media:thumbnail", NS):
                u = el.get("url"); ty = el.get("type", "") or el.get("medium", "")
                if u: (vids if "video" in ty else imgs).append(u)
            imgs += re.findall(r'<img[^>]+src="([^"]+)"', desc)
            out.append({"id": "rss:" + hashlib.md5(link.encode()).hexdigest(), "game": game, "title": title, "url": link,
                        "text": strip_html(desc)[:3000], "images": imgs, "videos": vids, "youtube": [], "ts": _pubts(it)})
    return out

def enrich_from_page(item):
    try:
        h = requests.get(item["url"], timeout=20, headers=UA).text[:400000]
        for prop, key in (("og:image", "images"), ("og:video", "videos"), ("og:video:url", "videos")):
            for m in re.findall(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]+)"' % prop, h) + \
                     re.findall(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="%s"' % prop, h):
                m = html.unescape(m)
                if "youtube" in m: item["youtube"].append(m)
                elif m not in item[key]: item[key].append(m)
        item["youtube"] += re.findall(r'https://www\.youtube\.com/(?:embed/|watch\?v=)[\w-]{11}', h)[:2]
        if len(item["text"]) < 300:
            p = " ".join(strip_html(x) for x in re.findall(r"<p[^>]*>(.*?)</p>", h, re.S)[:15])
            item["text"] = (item["text"] + " " + p)[:3000]
    except Exception as e: log.info("page %s: %s", item["url"], e)
    item["youtube"] = list(dict.fromkeys(item["youtube"]))[:2]

def download(url, kind):
    lim = PHOTO_MAX if kind == "photo" else VIDEO_MAX
    try:
        r = requests.get(url, timeout=60, stream=True, headers=UA); r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if kind == "photo" and not ct.startswith("image/"): return None
        if kind == "video" and not ct.startswith("video/"): return None
        if "svg" in ct or "gif" in ct and kind == "photo": return None
        ext = {"image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}.get(ct.split(";")[0], ".jpg" if kind == "photo" else ".mp4")
        path = os.path.join(MEDIA_DIR, hashlib.md5(url.encode()).hexdigest() + ext); n = 0
        with open(path, "wb") as f:
            t_end = time.time() + 90
            for c in r.iter_content(65536):
                n += len(c)
                if time.time() > t_end: f.close(); os.remove(path); log.info("dl too slow %s", url); return None
                if n > lim: f.close(); os.remove(path); return None
                f.write(c)
        if n < 5000: os.remove(path); return None  # icons/pixels
        return {"type": kind, "path": path, "url": url}
    except Exception as e: log.info("dl %s: %s", url, e); return None

def collect_media(item, maxn=4):
    media = []
    for v in item["videos"][:2]:
        m = download(v, "video")
        if m: media.append(m)
    for u in item["images"]:
        if len(media) >= maxn: break
        m = download(u, "photo")
        if m and m["path"] not in [x["path"] for x in media]: media.append(m)
    return media

# ---------- gemini ----------
STYLE = ("Ты редактор Telegram-канала Lobby Line (мемы, новости и обновления про игры). Напиши ОДИН короткий пост "
 "на русском (до 700 символов) в живом, дерзком, но дружелюбном стиле канала, с подходящими эмодзи и 1-2 хэштегами. "
 "СТРОГО только факты из источника ниже, ничего не выдумывай (никаких дат, цифр, цен, которых нет в тексте). "
 "Формат: жирный заголовок в <b></b> (Telegram HTML), 2-4 строки сути, в конце строка '🔗 <a href=\"URL\">Источник</a>'"
 "{yt}. Только HTML-теги b,i,a. Выведи только текст поста.")
def _gemini_try(models, prompt):
    last = None
    for m in models:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                headers={"x-goog-api-key": GKEY}, json={"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.9}}, timeout=45)
            if r.status_code == 200:
                parts = r.json()["candidates"][0]["content"]["parts"]
                txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if txt: return txt, m, None
                last = "empty"
            else: last = r.status_code
            log.warning("gemini %s -> %s", m, last)
        except Exception as e: last = type(e).__name__; log.warning("gemini %s: %s", m, last)
    return None, None, last

_BAD = ("tts", "image", "lite", "omni", "transcribe", "robotics", "computer-use", "banana")
def discover_flash_models():
    """Re-fetch ListModels and return text flash models, newest first."""
    import re
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": GKEY}, params={"pageSize": 1000}, timeout=30)
        r.raise_for_status()
        names = [x["name"].split("/", 1)[1] for x in r.json().get("models", [])
                 if "generateContent" in x.get("supportedGenerationMethods", [])]
    except Exception as e:
        log.warning("ListModels failed: %s", type(e).__name__); return []
    fl = [n for n in names if n.startswith("gemini") and "flash" in n and not any(b in n for b in _BAD)]
    def ver(n):
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (float(v.group(1)) if v else 0.0, "preview" not in n)
    fl.sort(key=ver, reverse=True)
    lite = sorted([n for n in names if n.startswith("gemini") and "flash-lite" in n
                   and not any(b in n for b in ("tts", "image"))], key=ver, reverse=True)
    return fl + lite

def gemini(prompt):
    global MODELS
    txt, m, last = _gemini_try(MODELS, prompt)
    if txt: return txt, m
    found = [x for x in discover_flash_models() if x not in MODELS]
    log.warning("all configured models failed (%s); trying discovered: %s", last, found)
    txt, m, last2 = _gemini_try(found, prompt)
    if txt:
        MODELS = [m] + [x for x in MODELS if x != m]
        return txt, m
    raise RuntimeError(f"all gemini models failed: {last2 or last}")

LITE = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite", "gemini-flash-lite-latest"]
_LITE_OK = {"t": 0, "l": None}
def lite_models():
    """Flash-lite models present in ListModels (cached 6h), preferred order."""
    if _LITE_OK["l"] is None or time.time() - _LITE_OK["t"] > 21600:
        found = [m for m in discover_flash_models() if "lite" in m]
        _LITE_OK["l"] = [m for m in LITE if m in found] + [m for m in found if m not in LITE] if found else list(LITE[:2])
        _LITE_OK["t"] = time.time()
    return _LITE_OK["l"]
def gemini_lite(prompt):
    """Cheap tasks: flash-lite first, then the normal flash chain."""
    txt, m, last = _gemini_try(lite_models(), prompt)
    if txt: return txt, m
    return gemini(prompt)

def write_post(item, extra=""):
    yt = f" и строка '▶️ <a href=\"{item['youtube'][0]}\">Трейлер</a>'" if item["youtube"] else ""
    p = STYLE.format(yt=yt) + extra + f"\n\nИгра: {item['game']}\nЗаголовок: {item['title']}\nURL: {item['url']}\nТекст источника:\n{item['text']}"
    text, model = gemini(p)
    text = re.sub(r"^```\w*|```$", "", text).strip()
    return text, model

NEWS_PER_GAME_DAY = 2; NEWS_FRESH = 48 * 3600
def news_day(s):
    today = dt.datetime.now(MSK).date().isoformat(); nd = s.get("news_day") or {}
    if nd.get("date") != today: nd = {"date": today, "counts": {}}
    s["news_day"] = nd; return nd
def gather(n=2):
    """Fair rotation: one fresh item per game, games ordered by longest time without a draft, max 2 per game per day."""
    s = load_state(); used = set(s["used"])
    used |= {"steam:" + x[len("upd:steam:"):] for x in s.get("upd_seen", []) if x.startswith("upd:steam:")}  # patch notes go via update flow
    items = [i for i in fetch_steam() + fetch_rss() if i["id"] not in used and i["url"] and time.time() - i["ts"] < NEWS_FRESH]
    counts = news_day(s)["counts"]; last = s.get("news_last", {})
    by_game = {}
    for i in sorted(items, key=lambda i: -i["ts"]): by_game.setdefault(i["game"], i)
    order = sorted(by_game, key=lambda g: last.get(g, 0))
    picked = [by_game[g] for g in order if counts.get(g, 0) < NEWS_PER_GAME_DAY][:n]
    log.info("news rotation: fresh=%s picked=%s", {g: counts.get(g, 0) for g in by_game}, [i["game"] for i in picked])
    return picked

def make_draft(item):
    enrich_from_page(item)
    text, model = write_post(item)
    media = collect_media(item)
    did = hashlib.md5((item["id"] + str(time.time())).encode()).hexdigest()[:10]
    return {"id": did, "item": item, "text": text, "media": media, "model": model, "status": "pending"}


# ---------- official updates / patch notes ----------
UPD_RE = re.compile(r"(?i)\b(update|patch|hotfix|release notes|changelog|обновлени|патч)|\b\d+\.\d+(\.\d+)?\b")
UPD_EVERY = 1800
def _steam_media(body):
    imgs = re.findall(r'\[img\](.*?)\[/img\]', body) + re.findall(r'<img[^>]+src="([^"]+)"', body)
    imgs = [i.replace("{STEAM_CLAN_IMAGE}", "https://clan.akamai.steamstatic.com/images") for i in imgs]
    yt = re.findall(r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+)', body)
    yt += ["https://youtu.be/" + v for v in re.findall(r'\[previewyoutube=([\w-]+)', body)]
    return imgs, yt

def upd_steam():
    out = []
    for game, appid in STEAM.items():
        try:
            j = requests.get("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                params={"appid": appid, "count": 15, "maxlength": 0, "format": "json"}, timeout=20, headers=UA).json()
            for it in j["appnews"]["newsitems"]:
                tags = it.get("tags") or []
                official = it.get("feedname") == "steam_community_announcements"
                if not ("patchnotes" in tags or (official and UPD_RE.search(it["title"]))): continue
                if re.search(r"(?i)store update|shop update", it["title"]): continue
                body = it.get("contents", ""); imgs, yt = _steam_media(body)
                out.append({"id": "upd:steam:" + it["gid"], "game": game, "title": it["title"], "url": it["url"],
                            "text": strip_html(re.sub(r"\[/?[^\]]+\]", " ", body))[:3500],
                            "images": imgs, "videos": [], "youtube": yt, "ts": it["date"]})
        except Exception as e: log.warning("upd steam %s: %s", game, e)
    return out

def _iso(t): return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()

def upd_valorant():
    out = []
    try:
        h = requests.get("https://playvalorant.com/en-us/news/tags/patch-notes/", timeout=25, headers=UA).text
        data = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S).group(1))
        def walk(o):
            if isinstance(o, dict):
                if "Patch Notes" in str(o.get("title", "")) and o.get("publishedAt") and isinstance(o.get("action"), dict):
                    yield o
                for v in o.values(): yield from walk(v)
            elif isinstance(o, list):
                for v in o: yield from walk(v)
        seen = set()
        for o in walk(data):
            u = o["action"].get("payload", {}).get("url", "")
            if not u or u in seen: continue
            seen.add(u); url = u if u.startswith("http") else "https://playvalorant.com" + u
            img = (o.get("media") or o.get("imageMedia") or {}).get("url")
            out.append({"id": "upd:val:" + u.rstrip("/").split("/")[-1], "game": "Valorant", "title": o["title"], "url": url,
                        "text": strip_html((o.get("description") or {}).get("body", "")), "images": [img] if img else [],
                        "videos": [], "youtube": [], "ts": _iso(o["publishedAt"])})
    except Exception as e: log.warning("upd valorant: %s", e)
    return out

def upd_hearthstone():
    out = []
    try:
        j = requests.get("https://hearthstone.blizzard.com/en-us/api/blog/articleList/",
                         params={"page": 1, "pageSize": 20}, timeout=25, headers=UA).json()
        for a in j:
            if not re.search(r"(?i)patch notes|hotfix", a.get("title", "")): continue
            t = (a.get("publish_details") or {}).get("time") or a.get("created_at")
            body = a.get("content") or ""
            imgs = re.findall(r'<img[^>]+src="([^"]+)"', body)
            th = a.get("thumbnail") or a.get("header") or {}
            if isinstance(th, dict) and th.get("url"): imgs.insert(0, th["url"])
            imgs = ["https:" + i if i.startswith("//") else i for i in imgs]
            out.append({"id": f"upd:hs:{a['id']}", "game": "Hearthstone", "title": a["title"],
                        "url": a.get("defaultUrl") or f"https://hearthstone.blizzard.com/en-us/news/{a['id']}",
                        "text": strip_html(body)[:3500], "images": imgs, "videos": [], "youtube": [], "ts": _iso(t)})
    except Exception as e: log.warning("upd hearthstone: %s", e)
    return out

def fetch_updates():
    return upd_steam() + upd_valorant() + upd_hearthstone()

def latest_updates_text(items=None):
    items = fetch_updates() if items is None else items
    best = {}
    for i in items:
        if i["game"] not in best or i["ts"] > best[i["game"]]["ts"]: best[i["game"]] = i
    if not best: return "Не удалось получить обновления 🤷"
    lines = ["🛠 <b>Последние официальные обновления</b>"]
    for g in sorted(best, key=lambda g: -best[g]["ts"]):
        i = best[g]; d = dt.datetime.fromtimestamp(i["ts"], TZ).strftime("%d.%m.%Y")
        lines.append(f"• <b>{html.escape(g)}</b> — {d}: <a href=\"{html.escape(i['url'])}\">{html.escape(i['title'])}</a>")
    return "\n".join(lines)

UPD_STYLE = (" Это ОФИЦИАЛЬНОЕ ОБНОВЛЕНИЕ (патчноут). Первая строка поста строго: <b>🛠 Обновление {game}</b>. "
             "Дальше кратко 3-6 главных изменений списком (строки с • ), только из текста источника.")
def make_update_draft(item):
    enrich_from_page(item)
    text, model = write_post(item, UPD_STYLE.format(game=item["game"]))
    media = collect_media(item)
    did = hashlib.md5((item["id"] + str(time.time())).encode()).hexdigest()[:10]
    return {"id": did, "item": item, "text": text, "media": media, "model": model, "status": "pending", "kind": "update"}

def update_cycle():
    items = fetch_updates()  # network: never under the state lock
    with lock:
        s = load_state(); seen = set(s.get("upd_seen", []))
        new = [i for i in items if i["id"] not in seen]
        if not s.get("upd_init"):
            # first run: mark everything seen, draft only newest per game if < 48h old
            best = {}
            for i in items:
                if i["game"] not in best or i["ts"] > best[i["game"]]["ts"]: best[i["game"]] = i
            todo = [i for i in best.values() if time.time() - i["ts"] < 48 * 3600]
            s["upd_seen"] = list(seen | {i["id"] for i in items})[-3000:]; s["upd_init"] = True; save_state(s)
            log.info("updates init: %d seen, %d to draft", len(items), len(todo))
        else:
            todo = sorted(new, key=lambda i: i["ts"])
            if todo:
                s["upd_seen"] = (s.get("upd_seen", []) + [i["id"] for i in todo])[-3000:]; save_state(s)
    for it in todo:
        try: d = make_update_draft(it)
        except Exception: log.exception("update draft failed"); continue
        with lock:
            s = load_state(); s["drafts"][d["id"]] = d; save_state(s)
        draft_to_owner(d)

def update_loop():
    while True:
        run_job("updates", update_cycle)
        time.sleep(UPD_EVERY)

# ---------- sending ----------
def kb(did):
    return {"inline_keyboard": [[{"text": "✅ Канал + чат", "callback_data": f"pub:{did}"}, {"text": "💬 Только чат", "callback_data": f"chat:{did}"}],
             [{"text": "✏️ Переписать", "callback_data": f"re:{did}"}, {"text": "❌ Отклонить", "callback_data": f"no:{did}"}]]}

def send_post(chat, d, markup=None, extra=None):
    """Send media+caption (or text). Returns True on success. markup attached to last message."""
    X = extra or {}
    text, media = d["text"], [m for m in d["media"] if os.path.exists(m["path"])]
    short = len(re.sub(r"<[^>]+>", "", text)) <= 1024
    if not media:
        return tg("sendMessage", **X, chat_id=chat, text=text, parse_mode="HTML", reply_markup=markup or {}).get("ok")
    if len(media) == 1:
        m = media[0]; meth, field = ("sendPhoto", "photo") if m["type"] == "photo" else ("sendVideo", "video")
        params = dict(**X, chat_id=chat, parse_mode="HTML")
        if short: params["caption"] = text
        if markup and short: params["reply_markup"] = markup
        with open(m["path"], "rb") as f: j = tg(meth, files={field: f}, **params)
        if not j.get("ok"): d["media"] = []; return send_post(chat, d, markup, extra)
        if not short: return tg("sendMessage", **X, chat_id=chat, text=text, parse_mode="HTML", reply_markup=markup or {}).get("ok")
        return True
    files, group = {}, []
    for i, m in enumerate(media[:10]):
        files[f"f{i}"] = open(m["path"], "rb")
        e = {"type": m["type"], "media": f"attach://f{i}"}
        if i == 0 and short: e.update(caption=text, parse_mode="HTML")
        group.append(e)
    try: j = tg("sendMediaGroup", **X, files=files, chat_id=chat, media=group)
    finally: [f.close() for f in files.values()]
    if not j.get("ok"): d["media"] = media[:1]; return send_post(chat, d, markup, extra)
    if not short: tg("sendMessage", **X, chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if markup: tg("sendMessage", **X, chat_id=chat, text="👆 Черновик выше. Что делаем?", reply_markup=markup)
    return True

def draft_to_owner(d):
    s = load_state(); owner = s["owner"]
    if not owner: log.info("no owner yet; draft %s kept", d["id"]); return
    tg("sendMessage", chat_id=owner, text=f"📝 Черновик ({d['item']['game']}, медиа: {len(d['media'])})")
    send_post(owner, d, kb(d["id"]))

def run_cycle(chat=None, n=2):
    with job_lock:
        items = gather(n)
        if not items:
            if chat: tg("sendMessage", chat_id=chat, text="Свежих новостей пока нет 🤷")
            return
        for it in items:
            try: d = make_draft(it)
            except Exception as e: log.exception("draft failed"); continue
            with lock:
                s = load_state(); s["used"] = (s["used"] + [it["id"]])[-2000:]; s["drafts"][d["id"]] = d
                nd = news_day(s); nd["counts"][it["game"]] = nd["counts"].get(it["game"], 0) + 1
                s.setdefault("news_last", {})[it["game"]] = time.time(); save_state(s)
            draft_to_owner(d)


# ---------- group / topics ----------
MSK = ZoneInfo("Europe/Moscow")
def norm_game(name):
    t = (name or "").lower().strip()
    for g, keys in GAMES.items():
        if t == g.lower() or t.replace(" ", "") == g.lower().replace(" ", "") or any(k in t for k in keys): return g
    if t in ("cs", "cs2", "кс"): return "CS 2"

def topic_of(s, game): return s.get("topics", {}).get(game)

def flush_queue(s):
    """Post approved items whose topic is now known. Caller saves state."""
    grp = s.get("group")
    if not grp: return
    for m in s.get("memes", {}).values():
        th = topic_of(s, m["game"])
        if m["status"] == "queued" and th and post_meme(grp, th, m): m["status"] = "posted"
    for d in s.get("drafts", {}).values():
        th = topic_of(s, d["item"]["game"])
        if d.get("chat_status") == "queued" and th and send_post(grp, dict(d), extra={"message_thread_id": th}):
            d["chat_status"] = "posted"

def handle_group(msg, s):
    chat = msg["chat"]; owner = s.get("owner"); changed = False
    if chat.get("is_forum") and s.get("group") != chat["id"]:
        s["group"] = chat["id"]; s["group_title"] = chat.get("title"); changed = True; log.info("group found %s", chat["id"])
    th = msg.get("message_thread_id")
    ftc = msg.get("forum_topic_created") or msg.get("forum_topic_edited")
    if ftc and th and not ftc.get("name") and ftc.get("icon_custom_emoji_id") and chat["id"] == s.get("group"):
        e = next((x for x in existing_topics(s).values() if x.get("th") == th), None)
        if e: e["icon"] = ftc["icon_custom_emoji_id"]; changed = True
    if ftc and th and ftc.get("name") and chat["id"] == s.get("group"):
        et = existing_topics(s)
        old = next((k for k, x in et.items() if x.get("th") == th), None)  # renamed topic
        if old and old != ftc["name"].strip() and msg.get("forum_topic_edited"): et[ftc["name"].strip()] = et.pop(old)
        e = et.setdefault(ftc["name"].strip(), {"th": th, "aliases": []}); e["th"] = th
        if ftc.get("icon_custom_emoji_id"): e["icon"] = ftc["icon_custom_emoji_id"]
        changed = True; s["stats_dirty"] = True
    if ftc and th and ftc.get("name"):
        g = norm_game(ftc["name"])
        if g: s.setdefault("topics", {})[g] = th; changed = True; log.info("topic %s -> %s", g, th)
    rt = msg.get("reply_to_message", {}).get("forum_topic_created")
    if rt and th and rt.get("name") and chat["id"] == s.get("group") and msg.get("is_topic_message"):
        et = existing_topics(s); nm = rt["name"].strip()
        k = next((k for k in et if _tk(k) == _tk(nm)), nm); e = et.setdefault(k, {"th": None, "aliases": []})
        if e.get("th") != th or (rt.get("icon_custom_emoji_id") and not e.get("icon")):
            e["th"] = th; e["icon"] = e.get("icon") or rt.get("icon_custom_emoji_id"); changed = True; s["stats_dirty"] = True
            log.info("topic learned from message: %s -> %s", k, th)
    if rt and th:
        g = norm_game(rt.get("name"))
        if g and topic_of(s, g) != th: s.setdefault("topics", {})[g] = th; changed = True; log.info("topic(reply) %s -> %s", g, th)
    txt = msg.get("text", "") or ""; frm = msg.get("from", {}).get("id")
    if msg.get("sender_chat", {}).get("id") == chat["id"]: frm = owner  # anonymous admin
    if frm == owner and txt.startswith("/"):
        cmd, _, arg = txt.partition(" "); cmd = cmd.split("@")[0]
        if cmd == "/bindgroup":
            s["group"] = chat["id"]; s["group_title"] = chat.get("title"); changed = True
            tg("sendMessage", chat_id=owner, text=f"✅ Группа привязана: {chat.get('title')} ({chat['id']})")
        elif cmd == "/settopic":
            g = norm_game(arg)
            if not th: tg("sendMessage", chat_id=owner, text="/settopic нужно отправить внутри темы")
            elif not g: tg("sendMessage", chat_id=owner, text="Не понял игру. Варианты: " + ", ".join(GAMES))
            else:
                s["group"] = chat["id"]; s.setdefault("topics", {})[g] = th; changed = True
                tg("sendMessage", chat_id=owner, text=f"✅ Тема {th} → {g}")
        elif cmd == "/votepost":
            cmd_votepost(msg, s); changed = True
        elif cmd == "/topics":
            if th:
                g = norm_game(arg) or norm_game(msg.get("reply_to_message", {}).get("forum_topic_created", {}).get("name"))
                if g: s.setdefault("topics", {})[g] = th; changed = True
            tg("sendMessage", chat_id=owner, text=topics_text(s))
        if cmd in ("/bindgroup", "/settopic", "/topics", "/votepost"):
            tg("deleteMessage", chat_id=chat["id"], message_id=msg["message_id"])
    if changed:
        rf = refund_topic_votes(s)
        if rf: log.info("refunded votes after topic update: %s", rf)
        flush_queue(s); save_state(s)

def topics_cmd(chat, txt):
    parts = txt.split(maxsplit=2); act = parts[1].lower() if len(parts) > 1 else ""; arg = parts[2].strip() if len(parts) > 2 else ""
    with lock:
        s = load_state(); et = existing_topics(s)
        if act in ("add", "добавить") and arg:
            m = re.match(r"(.+?)\s+(\d+)(?:\s+(\d{10,}))?$", arg); name, th, ic = (m.group(1), int(m.group(2)), m.group(3)) if m else (arg, None, None)
            name = next((k for k in et if _tk(k) == _tk(name)), name); old = et.get(name) or {}
            et[name] = {"th": th or old.get("th"), "aliases": old.get("aliases", []), "icon": ic or old.get("icon")}
            msg = f"✅ Добавлено: {name}" + (f" (тема {et[name]['th']})" if et[name]["th"] else " — без ссылки, укажите id темы")
        elif act in ("del", "remove", "удалить") and arg:
            k = next((k for k in et if _tk(k) == _tk(arg)), None)
            if k: del et[k]; (s.get("topics") or {}).pop(k, None)
            msg = f"🗑 Удалено: {k}" if k else f"Не нашёл «{arg}»"
        else: msg = None
        s["stats_dirty"] = True; save_state(s)
    lines = ["Игры с темами (не участвуют в голосовании):"] + [f"• {k}" + (f" — {topic_link(s, e['th'])}" if e.get("th") else " — ⚠️ нет id темы, ссылки не будет") + (" · иконка ✓" if e.get("icon") else "") for k, e in sorted(et.items())]
    lines += ["", "/topics add Название id_темы [id_иконки] — добавить/обновить", "/topics del Название — убрать",
              "id темы: откройте тему → «Копировать ссылку», последнее число в ссылке."]
    tg("sendMessage", chat_id=chat, text=((msg + "\n\n") if msg else "") + "\n".join(lines))

def topics_text(s):
    t = s.get("topics", {})
    lines = [f"Группа: {s.get('group_title') or '—'} ({s.get('group') or 'не найдена — отправьте /bindgroup в группе'})"]
    lines += [f"{'✅' if g in t else '❔'} {g}: {t.get(g, 'нет — /settopic ' + g + ' внутри темы')}" for g in GAMES]
    q = sum(1 for m in s.get("memes", {}).values() if m["status"] == "queued")
    if q: lines.append(f"В очереди ждут темы: {q}")
    return "\n".join(lines)

# ---------- memes ----------
MEME_SRC = {  # (telegram channels, pikabu tags, joyreactor tags, 9gag tags[last resort])
  "Dota 2": (["roflodota", "lesdoto"], ["Dota 2"], ["Dota 2"], ["dota-2"]),
  "CS 2": ([], ["Counter-strike"], ["Counter-Strike"], ["cs2"]),
  "Fortnite": ([], ["Fortnite"], ["Fortnite"], ["fortnite"]),
  "Valorant": ([], ["Valorant"], ["Valorant"], ["valorant"]),
  "World of Tanks": ([], ["World of Tanks"], ["World of Tanks"], ["worldoftanks"]),
  "Hearthstone": ([], ["Hearthstone"], ["Hearthstone"], ["hearthstone"]),
  "Overwatch": ([], ["Overwatch"], ["Overwatch"], ["overwatch"]),
  "The Finals": ([], ["The Finals"], [], []),
  "GTA 5": (["gta_ez"], ["GTA 5", "GTA"], ["GTA 5", "GTA"], ["gta-5"])}
BUA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
MEME_HOURS = (11, 16, 21)  # three chances a day, best meme only
SRC_STATUS = {}

def reddit_posts(sub):
    for host in ("www.reddit.com", "old.reddit.com"):
        try:
            r = requests.get(f"https://{host}/r/{sub}/hot.json", params={"limit": 40, "raw_json": 1},
                             headers={"User-Agent": "linux:lobbyline:1.0"}, timeout=15, allow_redirects=False)
            if r.status_code != 200: SRC_STATUS[f"reddit:{host}"] = r.status_code; continue
            SRC_STATUS[f"reddit:{host}"] = 200; out = []
            for c in r.json()["data"]["children"]:
                p = c["data"]
                if p.get("over_18") or p.get("spoiler") or p.get("stickied"): continue
                u = p.get("url_overridden_by_dest") or p.get("url", "")
                if not re.search(r"\.(jpe?g|png|gif|webp)$", u.split("?")[0], re.I): continue
                out.append({"id": "rd:" + p["id"], "title": p["title"], "img": u, "kind": "animation" if u.lower().endswith(".gif") else "photo",
                            "author": "u/" + p.get("author", "?"), "url": "https://www.reddit.com" + p["permalink"], "src": f"r/{sub}"})
            return out
        except Exception as e: SRC_STATUS[f"reddit:{host}"] = type(e).__name__
    return []

def ninegag_posts(tag):
    try:
        j = requests.get(f"https://9gag.com/v1/tag-posts/tag/{tag}/type/hot", headers=BUA, timeout=15).json()
        SRC_STATUS["9gag"] = 200
    except Exception as e: SRC_STATUS["9gag"] = type(e).__name__; return []
    out = []
    for p in j.get("data", {}).get("posts", []):
        if p.get("nsfw") or p.get("promoted") or p.get("type") not in ("Photo", "Animated"): continue
        im = p.get("images", {})
        if p["type"] == "Animated":
            v = (im.get("image460sv") or {}).get("url") or (im.get("image460svwm") or {}).get("url")
            if not v: continue
            img, kind = v, "animation"
        else:
            img, kind = (im.get("image700") or im.get("image460") or {}).get("url"), "photo"
        if not img: continue
        cr = p.get("creator") or {}
        out.append({"id": "9g:" + p["id"], "title": html.unescape(p.get("title", "")), "img": img, "kind": kind,
                    "author": cr.get("username") or "9GAG", "url": p.get("url", "").replace("http://", "https://"), "src": f"9gag #{tag}", "ru": False})
    return out

def tg_channel_posts(ch):
    try: h = requests.get(f"https://t.me/s/{ch}", headers=BUA, timeout=15).text; SRC_STATUS["telegram"] = 200
    except Exception as e: SRC_STATUS["telegram"] = type(e).__name__; return []
    out = []
    for blk in h.split('class="tgme_widget_message_wrap')[1:][::-1]:
        pid = re.search(r'data-post="([^"]+)"', blk)
        imgs = re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", blk)
        if not pid or len(imgs) != 1: continue
        txt = strip_html((re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', blk, re.S) or [None, ""])[1])
        if re.search(r"(?i)реклам|erid|промокод|подпишись|розыгрыш|t\.me/\+", txt): continue
        vw = re.search(r'tgme_widget_message_views">([\d.,]+)([KM]?)', blk)
        views = float(vw.group(1).replace(",", ".")) * {"": 1, "K": 1e3, "M": 1e6}[vw.group(2)] if vw else 0
        reacts = sum(int(x) for x in re.findall(r'class="tgme_reaction(?! tgme_reaction_paid)[^"]*">.*?</i>(\d+)</span>', blk))
        fwd = sum(int(x) for x in re.findall(r'forwards[^>]*>(\d+)<', blk))
        out.append({"id": "tg:" + pid.group(1), "title": txt[:200], "img": imgs[0], "kind": "photo", "pop": views / 100 + reacts * 3 + fwd * 5,
                    "author": "@" + ch, "url": "https://t.me/" + pid.group(1), "src": "t.me/" + ch, "ru": True})
    return out

BAD_TAGS = ("Клубничка", "NSFW", "Эротика", "Политика", "Реклама")
def pikabu_posts(tag):
    try:
        r = requests.get("https://pikabu.ru/tag/" + requests.utils.quote(tag) + "/hot", headers=BUA, timeout=20)
        h = r.content.decode("cp1251", "ignore"); SRC_STATUS["pikabu"] = r.status_code
    except Exception as e: SRC_STATUS["pikabu"] = type(e).__name__; return []
    out = []
    for a in re.findall(r'<article class="story[^"]*"(.*?)</article>', h, re.S):
        sid = re.search(r'data-story-id="(\d+)"', a); link = re.search(r'href="(https://pikabu\.ru/story/[^"]+)"', a)
        tags = re.findall(r'data-tag="([^"]+)"', a)
        if not sid or not link or any(t in BAD_TAGS for t in tags): continue
        if not any(t in ("Юмор", "Мемы", "Игровой юмор", "Картинка с текстом", "Комиксы") for t in tags): continue
        imgs = list(dict.fromkeys(re.findall(r'data-large-image="([^"]+)"', a)))
        if len(imgs) != 1: continue
        title = html.unescape((re.search(r'story__title-link[^>]*>([^<]*)', a) or [None, ""])[1])
        au = re.search(r'data-author-name="([^"]+)"', a); rt = re.search(r'data-rating="(-?\d+)"', a)
        out.append({"id": "pk:" + sid.group(1), "title": title, "img": imgs[0], "kind": "photo", "pop": int(rt.group(1)) if rt else 0,
                    "author": "@" + (au.group(1) if au else "pikabu"), "url": link.group(1), "src": "Пикабу", "ru": True})
    return out

def joy_posts(tag):
    try:
        r = requests.get("https://joyreactor.cc/tag/" + requests.utils.quote(tag) + "/best", headers=BUA, timeout=20)
        h = r.text; SRC_STATUS["joyreactor"] = r.status_code
    except Exception as e: SRC_STATUS["joyreactor"] = type(e).__name__; return []
    ms = list(re.finditer(r'href="/post/(\d+)"', h)); first = {}
    for m in ms: first.setdefault(m.group(1), m.start())
    ids = sorted(first, key=first.get); out = []
    for i, pid in enumerate(ids):
        seg = h[first[pid]: first[ids[i + 1]] if i + 1 < len(ids) else len(h)]
        imgs = list(dict.fromkeys(html.unescape(u) for u in re.findall(r'(https://img\d+\.joyreactor\.cc/pics/post/[^"\s]+)', seg) if "/full/" not in u))
        if len(imgs) != 1 or not re.search(r"\.(jpe?g|png|gif|webp)$", imgs[0], re.I): continue
        rt = re.search(r'post-rating[^>]*>\s*<div[^>]*>\s*([+-]?)(?:<!-- -->)?([\d.]+)', seg)
        pop = (-1 if rt and rt.group(1) == "-" else 1) * float(rt.group(2)) if rt else 0
        out.append({"id": "jr:" + pid, "title": "", "img": imgs[0], "pop": pop, "kind": "animation" if imgs[0].lower().endswith(".gif") else "photo",
                    "author": "JoyReactor", "url": f"https://joyreactor.cc/post/{pid}", "src": "JoyReactor", "ru": True})
    return out

def _top_share(posts, share=0.2):
    """Only the top `share` of a source page by popularity, and at least 1.1x the page median."""
    rated = [p for p in posts if p.get("pop", 0) > 0]
    if len(rated) < 3: return []
    vals = sorted(p["pop"] for p in rated); med = vals[len(vals) // 2]
    cut = max(vals[int(len(vals) * (1 - share))], med * 1.1 if med > 0 else 0)
    for p in rated: p["rel"] = p["pop"] / med if med else 1
    return sorted([p for p in rated if p["pop"] >= cut], key=lambda p: -p["pop"])
def meme_candidates(game, seen):
    chans, pk, jr, gg = MEME_SRC[game]  # 9GAG dropped: English, no reliable rating
    for fn, keys in ((tg_channel_posts, chans), (pikabu_posts, pk), (joy_posts, jr)):
        for k in keys:
            for p in _top_share(fn(k)):
                if p["id"] not in seen and p["img"] not in seen: yield dict(p, game=game)

VISION_Q = ('Ты строгий редактор игрового Telegram-канала. Оцени картинку-мем про {game}. Ответь строго JSON без пояснений: '
  '{{"has_text": bool (есть ли на картинке читаемый текст, кроме мелких водяных знаков/ников), "russian": bool (весь основной текст на русском), '
  '"english": bool (есть английские фразы, кроме названий игр/героев), "nsfw": bool, "political": bool, '
  '"watermark_or_ad": bool (заметный водяной знак, логотип паблика, реклама, QR, ссылки), '
  '"funny": 1-10, "clear": 1-10 (понятно без контекста), "quality": 1-10 (чёткость, не пережато), "relevant": 1-10 (про {game}), '
  '"fresh": 1-10 (не заезженный баян), "overall": 1-10 (стоит ли публиковать в топовом канале; 8+ только для реально смешного)}}')
VISION_DOWN = [0]
def vision_check(m):
    if time.time() < VISION_DOWN[0]: return None
    path = m["path"]
    if m["kind"] == "animation" or path.endswith((".mp4", ".gif")):
        fr = path + ".frame.jpg"
        os.system(f"ffmpeg -loglevel quiet -y -ss 1 -i '{path}' -frames:v 1 '{fr}' || ffmpeg -loglevel quiet -y -i '{path}' -frames:v 1 '{fr}'")
        path = fr
        if not os.path.exists(fr): return None
    ext = os.path.splitext(path)[1].lower()
    mime = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    import base64
    data = base64.b64encode(open(path, "rb").read()).decode()
    for model in (lite_models() + MODELS)[:5]:
        for _ in range(1):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GKEY}, json={"contents": [{"parts": [{"text": VISION_Q.format(game=m["game"])},
                    {"inline_data": {"mime_type": mime, "data": data}}]}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}, timeout=60)
                if r.status_code == 200:
                    t = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"] if not p.get("thought"))
                    return json.loads(re.search(r"\{.*\}", t, re.S).group(0))
                log.info("vision %s -> %s", model, r.status_code)
                break
            except Exception as e: log.info("vision %s: %s", model, type(e).__name__); break
    VISION_DOWN[0] = time.time() + 1800; log.warning("vision unavailable for 30 min")
    return None

MEME_MIN_SCORE = 8
def meme_ok(m, v):
    if not v or v.get("nsfw") or v.get("political") or v.get("english") or v.get("watermark_or_ad"): return False
    try:
        if float(v.get("overall", 0)) < MEME_MIN_SCORE or min(float(v.get(k, 0)) for k in ("funny", "clear", "quality", "relevant")) < 6: return False
    except (TypeError, ValueError): return False
    if v.get("has_text"): return bool(v.get("russian"))
    return bool(m.get("ru"))

def fetch_meme(game, seen, check=True, max_tries=6):
    """First candidate that downloads and passes the Russian-language vision check."""
    for n, m in enumerate(meme_candidates(game, seen)):
        if n >= max_tries: break
        seen.add(m["id"]); m["path"] = meme_file(m)
        if not m["path"]: continue
        ok, why, h = image_gate(m["path"])
        if not ok: log.info("meme skipped %s %s: %s", game, m["url"], why); continue
        m["phash"] = h
        v = vision_check(m) if check else {"has_text": False}
        m["vision"] = v
        if v is None: continue  # no Gemini score -> no meme (quality first)
        if meme_ok(m, v): seen.add(m["img"]); m["score"] = float(v.get("overall", 0)); return m
        log.info("meme rejected %s %s %s", game, m["url"], v)

def _dhash(path):
    from PIL import Image
    im = Image.open(path); im.seek(0); g = im.convert("L").resize((17, 16), Image.LANCZOS); px = list(g.getdata())
    return sum(1 << i for i in range(256) if px[(i // 16) * 17 + i % 16] > px[(i // 16) * 17 + i % 16 + 1])
def image_gate(path):
    """Resolution >= 500 px on the short side and not a near-duplicate of a sent meme (perceptual dHash)."""
    try:
        from PIL import Image
        if path.endswith(".mp4"): return True, "", None
        im = Image.open(path)
        if min(im.size) < 500: return False, f"low-res {im.size}", None
        h = _dhash(path)
    except Exception as e: return False, f"bad image {type(e).__name__}", None
    for o in load_state().get("meme_hashes", []):
        if bin(h ^ int(o)).count("1") <= 10: return False, "near-duplicate", None
    return True, "", h

MEME_PROMPT = ("Придумай ОЧЕНЬ короткую смешную подпись СТРОГО на русском языке (никаких английских слов) (1-2 строки, до 150 символов, 1-2 эмодзи) к игровому мему "
  "для Telegram-чата по игре {game}. Заголовок/текст оригинала (может быть пустым): «{title}». Не выдумывай фактов. Без хэштегов, без кавычек, только текст подписи.")
def meme_caption(m):
    try:
        t, _ = gemini(MEME_PROMPT.format(game=m["game"], title=m["title"]))
        t = html.escape(re.sub(r"^```\w*|```$", "", t).strip().strip('"«»'))[:300]
    except Exception as e:
        log.warning("meme caption: %s", e); t = html.escape(m["title"])
    return f"{t}\n\n📷 {html.escape(m['author'])} · <a href=\"{html.escape(m['url'])}\">{html.escape(m['src'])}</a>"

def meme_file(m):
    try:
        r = requests.get(m["img"], headers=BUA, timeout=60); r.raise_for_status()
        if len(r.content) > (VIDEO_MAX if m["kind"] == "animation" else PHOTO_MAX) or len(r.content) < 3000: return None
        ext = os.path.splitext(m["img"].split("?")[0])[1] or ".jpg"
        p = os.path.join(MEDIA_DIR, "meme_" + hashlib.md5(m["img"].encode()).hexdigest() + ext)
        open(p, "wb").write(r.content); return p
    except Exception as e: log.info("meme dl %s: %s", m["img"], e)

def post_meme(chat, thread, m, markup=None):
    meth, field = ("sendAnimation", "animation") if m["kind"] == "animation" else ("sendPhoto", "photo")
    p = dict(chat_id=chat, caption=m["caption"], parse_mode="HTML")
    if thread: p["message_thread_id"] = thread
    if markup: p["reply_markup"] = markup
    if m.get("path") and os.path.exists(m["path"]):
        with open(m["path"], "rb") as f: j = tg(meth, files={field: f}, **p)
    else: j = tg(meth, **{field: m["img"]}, **p)
    return j.get("ok")

MEME_DAILY_MAX = 3
def meme_batch(chat=None):
    """Find the single best meme across games (popularity-ranked, Gemini score >= 8); max 3 per day; nothing if none passes."""
    if not job_lock.acquire(timeout=5):
        log.warning("meme batch skipped: another job is running"); return
    try:
        s = load_state(); owner = s.get("owner")
        if not owner: return
        today = dt.datetime.now(MSK).date().isoformat(); md = s.get("meme_day", {})
        sent = md.get("n", 0) if md.get("date") == today else 0
        if sent >= MEME_DAILY_MAX and not chat:
            log.info("meme daily cap reached"); return
        seen = set(s.get("meme_seen", [])); got = []; t0 = time.time()
        games = sorted(GAMES, key=lambda g: s.get("meme_last", {}).get(g, 0))
        for g in games:
            if time.time() - t0 > JOB_LIMIT: log.error("meme batch over %ss, stopping", JOB_LIMIT); break
            try: m = fetch_meme(g, seen, max_tries=3)
            except Exception: log.exception("meme %s", g); m = None
            if m: got.append(m)
            if len(got) >= 2: break
        got = sorted(got, key=lambda m: (-m.get("score", 0), -m.get("rel", 0)))[:1]
        with lock:
            s2 = load_state(); s2["meme_seen"] = list(set(s2.get("meme_seen", [])) | seen)[-5000:]
            for m in got:
                if m.get("phash") is not None: s2["meme_hashes"] = (s2.get("meme_hashes", []) + [str(m["phash"])])[-2000:]
                s2.setdefault("meme_last", {})[m["game"]] = time.time()
            if got: s2["meme_day"] = {"date": today, "n": sent + len(got)}
            save_state(s2)
    finally: job_lock.release()
    if not got:
        log.info("meme batch: nothing passed the quality bar")
        if chat: tg("sendMessage", chat_id=chat, text="Сейчас нет мемов, достойных публикации (нужна оценка 8+/10) 🤷")
        return
    for m in got: m["caption"] = meme_caption(m)
    for m in got:
        v = m.get("vision") or {}
        mid = hashlib.md5((m["id"] + str(time.time())).encode()).hexdigest()[:10]
        m.update(mid=mid, status="pending")
        prev = dict(m, caption=f"<b>[{html.escape(m['game'])}]</b> " + m["caption"] +
                    f"\n<i>Оценка {v.get('overall')}/10 · смешно {v.get('funny')} · качество {v.get('quality')} · {html.escape(m['src'])}</i>")
        with lock:
            s = load_state(); s.setdefault("memes", {})[mid] = m
            ms = s["memes"]
            if len(ms) > 600:
                for k in sorted(ms, key=lambda k: ms[k].get("ts", 0))[:len(ms) - 600]: ms.pop(k)
            m["ts"] = time.time(); save_state(s)
        tg("sendMessage", chat_id=owner, text=f"😂 Мем на одобрение ({dt.datetime.now(MSK):%H:%M} МСК). ✅ — в тему игры, ❌ — пропустить.")
        post_meme(owner, None, prev, {"inline_keyboard": [[{"text": "✅ В чат", "callback_data": f"mok:{mid}"},
                                                           {"text": "❌", "callback_data": f"mno:{mid}"}]]})

def meme_loop():
    done = set()
    while True:
        now = dt.datetime.now(MSK); key = now.strftime("%Y%m%d%H")
        if now.hour in MEME_HOURS and key not in done and load_state().get("meme_hour") != key:
            done.add(key)
            with lock:
                st = load_state(); st["meme_hour"] = key; save_state(st)
            run_job("memes", meme_batch)
        time.sleep(60)

# ---------- game voting ----------
VOTE_GOAL = 10
BASE_ALIASES = {"dota 2", "dota2", "cs 2", "cs2", "counter-strike 2", "fortnite", "valorant", "world of tanks", "hearthstone",
                "overwatch", "overwatch 2", "the finals", "gta 5", "gta v", "grand theft auto v", "grand theft auto v enhanced", "grand theft auto v legacy"}
def _nt(t): return re.sub(r"[^\w]+", " ", (t or "").lower()).strip()
def _tk(t): return re.sub(r"[\W_]+", "", (t or "").lower())
EXISTING_SEED = {"Dota 2": ["dota", "дота", "дота 2"], "Valorant": ["валорант"], "Fortnite": ["фортнайт"],
                 "The Finals": ["finals", "the finals", "зе финалс", "файналс"]}
def existing_topics(s):
    """{name: {"th": thread_id|None, "aliases": [...]}} — games that already have a topic and don't take part in voting."""
    et = s.setdefault("existing_topics", {})
    for k, al in EXISTING_SEED.items(): et.setdefault(k, {"th": None, "aliases": al})
    for k, th in (s.get("topics") or {}).items():
        e = et.setdefault(k, {"th": th, "aliases": []}); e["th"] = e.get("th") or th
    return et
def topic_entry(s, title):
    n = _tk(title)
    if not n: return None
    for k, e in existing_topics(s).items():
        if n == _tk(k) or n in {_tk(a) for a in e.get("aliases", [])}: return k, e
    for v in s.get("votes", {}).values():
        if v.get("status") == "created" and _tk(v["title"]) == n: return v["title"], {"th": (s.get("topics") or {}).get(v["title"])}
    return None
def has_topic(s, title, appid=None): return topic_entry(s, title) is not None
def topic_link(s, th):
    g = s.get("group")
    return f"https://t.me/c/{str(g)[4:] if str(g).startswith('-100') else str(g).lstrip('-')}/{th}" if g and th else None
def topic_button(s, title):
    """Inline URL button straight into the topic, or None if its thread id is unknown."""
    te = topic_entry(s, title)
    if not te: return None
    k, e = te; ln = topic_link(s, e.get("th"))
    return {"inline_keyboard": [[{"text": f"🎮 {k} → перейти", "url": ln}]]} if ln else None
def topic_jump_msg(chat, title):
    """Short notice with a jump button; removed after 60 s (vote not counted, wallet untouched)."""
    s = load_state(); te = topic_entry(s, title); k = te[0] if te else title; kb = topic_button(s, title)
    j = tg("sendMessage", chat_id=chat, text=f"У игры «{k}» уже есть своя тема в чате — голос не засчитан и не списан, можно проголосовать за другую игру.",
           disable_notification=True, **({"reply_markup": kb} if kb else {}))
    if j.get("ok"): del_later(chat, j["result"]["message_id"], 60)
def topic_notice(s, title):
    """HTML notice with the topic icon (custom emoji if known) and a direct link."""
    k, e = topic_entry(s, title); ln = topic_link(s, e.get("th")); ic = e.get("icon")
    icon = f'<tg-emoji emoji-id="{ic}">🎮</tg-emoji>' if ic else "🎮"
    name = html.escape(k)
    return f"{icon} Тема про <b>{name}</b> уже есть в чате 👉 " + (f'<a href="{ln}">открыть тему</a>' if ln else "загляните в список тем")
def epic_search(term):
    try:
        r = requests.get("https://store.epicgames.com/graphql", headers=BUA, timeout=6, params={
            "operationName": "searchStoreQuery",
            "variables": json.dumps({"keywords": term, "count": 5, "country": "RU", "locale": "ru", "category": "games/edition/base"}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "7d58e12d9dd8cb14c84a3ff18d360bf9f0caa96bf218f2c5fda68ba88d68a437"}})})
        if r.status_code != 200: return []
        els = r.json()["data"]["Catalog"]["searchStore"]["elements"]
        out = []
        for e in els:
            img = next((i["url"] for i in e.get("keyImages", []) if i.get("type") in ("Thumbnail", "OfferImageWide", "DieselStoreFrontWide")), None)
            out.append({"id": "e" + hashlib.md5(e["title"].lower().encode()).hexdigest()[:10], "title": e["title"], "img": img, "src": "Epic Games"})
        return out
    except Exception: return []
def steam_search(term):
    try:
        j = requests.get("https://store.steampowered.com/api/storesearch/", params={"term": term, "l": "russian", "cc": "RU"}, headers=UA, timeout=8).json()
        return [{"id": f"s{i['id']}", "appid": i["id"], "title": i["name"], "img": i.get("tiny_image"), "src": "Steam"} for i in j.get("items", []) if i.get("type") == "app"]
    except Exception: log.warning("steam search failed"); return []
def vote_markup(gid): return {"inline_keyboard": [[{"text": "Голосовать", "callback_data": f"v:{gid}"}]]}
def on_inline(q):
    if q.get("chat_type") not in ("group", "supergroup") and not is_allowed(load_state(), q["from"]["id"]):
        return tg("answerInlineQuery", inline_query_id=q["id"], results=[], cache_time=0, is_personal=True)
    term = (q.get("query") or "").strip()
    if len(term) < 2:
        return tg("answerInlineQuery", inline_query_id=q["id"], results=[], cache_time=5)
    found = steam_search(term) + epic_search(term)  # network outside the lock
    with lock:
        s = load_state()
        res, seen = [], set()
        for g in found:
            if _nt(g["title"]) in seen: continue
            seen.add(_nt(g["title"]))
            if has_topic(s, g["title"]):
                r = {"type": "article", "id": "t" + hashlib.md5(_tk(g["title"]).encode()).hexdigest()[:12], "title": g["title"],
                     "description": "✅ уже есть тема", "input_message_content": {"message_text": topic_notice(s, g["title"]), "parse_mode": "HTML", "disable_web_page_preview": True}}
                if topic_button(s, g["title"]): r["reply_markup"] = topic_button(s, g["title"])
                if g.get("img"): r["thumbnail_url"] = g["img"]
                res.append(r); continue
            # Epic duplicates of a Steam game share one vote bucket
            gid = next((k for k, v in s.get("votes", {}).items() if _nt(v["title"]) == _nt(g["title"])), g["id"])
            vg = s.setdefault("votes", {}).setdefault(gid, {"title": g["title"], "users": [], "status": "open"})
            if vg["status"] == "created": continue
            r = {"type": "article", "id": gid, "title": g["title"], "description": f"{g['src']} · голосов: {len(vg['users'])}",
                 "input_message_content": {"message_text": f"🗳 Голос за {g['title']}"}, "reply_markup": vote_markup(gid)}
            if g.get("img"): r["thumbnail_url"] = g["img"]
            res.append(r)
            if len(res) >= 20: break
        # drop never-voted entries beyond a cap to keep state small
        v = s["votes"]; empty = [k for k, x in v.items() if not x["users"]]
        for k in empty[:-300]: del v[k]
        save_state(s)
    j = tg("answerInlineQuery", inline_query_id=q["id"], results=res, cache_time=30, is_personal=True)
    if not j.get("ok") and "tg-emoji" in json.dumps(res, ensure_ascii=False):
        for r in res:
            imc = r.get("input_message_content", {})
            if "tg-emoji" in imc.get("message_text", ""): imc["message_text"] = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", imc["message_text"])
        tg("answerInlineQuery", inline_query_id=q["id"], results=res, cache_time=30, is_personal=True)

def top_text(s, n=10):
    gs = sorted([g for g in s.get("votes", {}).values() if g["users"] and g["status"] != "created"], key=lambda g: -len(g["users"]))[:n]
    if not gs: return "Пока голосов нет — будьте первыми!"
    return "\n".join(f"{i}. {html.escape(g['title'])} — {len(g['users'])}" for i, g in enumerate(gs, 1))
def votepost_text(s):
    return ("🗳 <b>Голосуем за новую игру</b>\n\nКак проголосовать: нажмите «🔍 Найти игру» (или наберите <code>@lobbyline_helper_bot название</code>), "
            "выберите игру и нажмите «Голосовать». Один голос на игру от человека.\n"
            f"Игра, набравшая {VOTE_GOAL} голосов, может получить свою тему в чате.\n\n<b>Топ-10:</b>\n" + top_text(s))
VP_MARKUP = {"inline_keyboard": [[{"text": "🔍 Найти игру", "switch_inline_query_current_chat": ""}]]}
VP_DIRTY = [False]
def votepost_loop():
    last = ""
    while True:
        time.sleep(15)
        if not VP_DIRTY[0]: continue
        VP_DIRTY[0] = False
        s = load_state(); vp = s.get("votepost")
        if not vp: continue
        t = votepost_text(s)
        if t != last:
            tg("editMessageText", chat_id=vp["chat"], message_id=vp["mid"], text=t, parse_mode="HTML", reply_markup=VP_MARKUP); last = t

def cmd_votepost(msg, s):
    chat = msg["chat"]; owner = s.get("owner")
    if msg.get("is_topic_message"):
        tg("sendMessage", chat_id=owner, text="/votepost нужно отправить в теме General (Основная)"); return
    if not s.get("group"): s["group"] = chat["id"]; s["group_title"] = chat.get("title")
    save_state(s); threading.Thread(target=publish_stats, daemon=True).start()
    tg("sendMessage", chat_id=owner, text="✅ Закреплённая таблица в General обновлена")

def on_chosen(r):
    """Picking a game only records the user's name; the vote is counted on «Голосовать»."""
    fu = r["from"]
    with lock:
        s = load_state(); s.setdefault("names", {})[str(fu["id"])] = ("@" + fu["username"]) if fu.get("username") else fu.get("first_name", "участник")
        save_state(s)

# ---- vote wallet: 1 vote, +1 every 14 days, up to VOTE_CAP ----
REFILL, VOTE_CAP = 14 * 86400, 3
def wallet(s, uid):
    ws = s.setdefault("wallets", {}); w = ws.get(str(uid))
    if w is None:
        w = ws[str(uid)] = {"avail": 1, "next": None, "last_refill": None, "hist": []}
    now = time.time()
    while w.get("next") and now >= w["next"] and w["avail"] < VOTE_CAP:
        w["avail"] += 1; w["last_refill"] = w["next"]; w["next"] = w["next"] + REFILL if w["avail"] < VOTE_CAP else None
    if w["avail"] >= VOTE_CAP: w["next"] = None
    return w
def next_str(w, short=False):
    if not w.get("next"): return "—"
    left = max(0, w["next"] - time.time()); dd, hh = int(left // 86400), int(left % 86400 // 3600)
    when = dt.datetime.fromtimestamp(w["next"], MSK).strftime("%d.%m %H:%M МСК")
    return f"{dd} дн. ({when})" if short else f"{dd} дн. {hh} ч. ({when})"
def myvote_text(s, uid):
    w = wallet(s, uid)
    if w["avail"] > 0 and not w.get("next"): return f"Голос доступен ✅\nДоступно голосов: {w['avail']} (максимум {VOTE_CAP})"
    t = f"Доступно голосов: {w['avail']}. Следующий через {next_str(w)}"
    return ("Голос доступен ✅\n" + t) if w["avail"] > 0 else t
def refund_topic_votes(s):
    """Votes for games that already have a topic don't count: remove them and give the votes back."""
    out = []
    for gid, g in s.get("votes", {}).items():
        if not g.get("users") or g.get("status") == "created" or not has_topic(s, g["title"]): continue
        for u in g["users"]:
            w = wallet(s, u); w["avail"] = min(VOTE_CAP, w["avail"] + 1)
            h = w.get("hist", [])
            i = next((i for i in range(len(h) - 1, -1, -1) if h[i].get("g") == gid), None)
            if i is not None: h.pop(i)
            if w["avail"] >= VOTE_CAP: w["next"] = None
        out.append(f"{g['title']}: {len(g['users'])}"); g["users"] = []; g["status"] = "topic"
    if out: s["stats_dirty"] = True; s["stats_at"] = 0
    return out
def migrate_wallets(s):
    if s.get("wallets_migrated"): return
    now = time.time()
    for gid, g in s.get("votes", {}).items():
        for u in g.get("users", []):
            w = s.setdefault("wallets", {}).setdefault(str(u), {"avail": 1, "next": None, "last_refill": None, "hist": []})
            w["avail"] = max(0, w["avail"] - 1); w["next"] = w.get("next") or now + REFILL; w["hist"].append({"g": gid, "t": int(now)})
    s["wallets_migrated"] = True

def on_vote(q, s, gid):
    s["stats_dirty"] = True
    g = s.get("votes", {}).get(gid); uid = q["from"]["id"]
    if not g: return tg("answerCallbackQuery", callback_query_id=q["id"], text="Игра не найдена, найдите её заново через поиск", show_alert=True)
    if g["status"] == "created" or has_topic(s, g["title"]):
        threading.Thread(target=bump_tag, args=(f"{uid}:{_nt(g['title'])}", 0), daemon=True).start()
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Эта игра уже есть в чатах — голос не списан, можно выбрать другую 👇")
        chat = (q.get("message") or {}).get("chat", {}).get("id") or s.get("group")
        threading.Thread(target=topic_jump_msg, args=(chat, g["title"]), daemon=True).start()
        return
    threading.Thread(target=bump_tag, args=(f"{uid}:{_nt(g['title'])}", 0), daemon=True).start()
    w = wallet(s, uid)
    if w["avail"] <= 0:
        save_state(s)
        return tg("answerCallbackQuery", callback_query_id=q["id"], text=f"У тебя нет голосов. Следующий через {next_str(w, short=True)}", show_alert=True)
    w["avail"] -= 1; now = time.time()
    if not w.get("next"): w["next"] = now + REFILL
    w.setdefault("hist", []).append({"g": gid, "t": int(now)}); w["hist"] = w["hist"][-50:]
    g["users"].append(uid); n = len(g["users"])
    fu = q["from"]; s.setdefault("names", {})[str(uid)] = ("@" + fu["username"]) if fu.get("username") else fu.get("first_name", "участник")
    if n >= VOTE_GOAL and not g.get("notified") and s.get("owner"):
        g["notified"] = True
        tg("sendMessage", chat_id=s["owner"], text=f"🎮 Игра «{g['title']}» набрала {n} голосов. Создать для неё тему в чате?",
           reply_markup={"inline_keyboard": [[{"text": "✅ Создать тему", "callback_data": f"vt:{gid}"}, {"text": "❌", "callback_data": f"vx:{gid}"}]]})
    save_state(s); VP_DIRTY[0] = True
    tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Голос учтён ✅\n{g['title']}: {n} гол.\nОсталось голосов: {w['avail']}", show_alert=True)
    t = f"🗳 Голос за {g['title']}\nГолосов: {n}"
    ref = {"inline_message_id": q["inline_message_id"]} if q.get("inline_message_id") else {"chat_id": q["message"]["chat"]["id"], "message_id": q["message"]["message_id"]}
    tg("editMessageText", text=t, reply_markup=vote_markup(gid), **ref)

def on_vote_owner(q, s, act, gid):
    g = s.get("votes", {}).get(gid); chat = q["from"]["id"]; mid = q["message"]["message_id"]
    if not g or g["status"] != "open": return tg("answerCallbackQuery", callback_query_id=q["id"], text="Уже обработано")
    if act == "vx":
        g["status"] = "rejected"; save_state(s)
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        return tg("answerCallbackQuery", callback_query_id=q["id"], text="Отклонено")
    grp = s.get("group")
    if not grp:
        tg("answerCallbackQuery", callback_query_id=q["id"])
        return tg("sendMessage", chat_id=chat, text="Группа не привязана: отправьте /bindgroup в группе Lobby Line Чат, затем снова нажмите «✅ Создать тему».")
    j = tg("createForumTopic", chat_id=grp, name=g["title"][:128])
    if not j.get("ok"):
        tg("answerCallbackQuery", callback_query_id=q["id"])
        return tg("sendMessage", chat_id=chat, text=f"Не удалось создать тему: {j.get('description')}")
    th = j["result"]["message_thread_id"]
    s.setdefault("topics", {})[g["title"]] = th; existing_topics(s)[g["title"]] = {"th": th, "aliases": []}; g["status"] = "created"; save_state(s); VP_DIRTY[0] = True
    tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    tg("answerCallbackQuery", callback_query_id=q["id"], text="Тема создана ✅")
    tg("sendMessage", chat_id=grp, text=f"🎉 По итогам голосования создана новая тема: «{g['title']}» ({len(g['users'])} голосов). Добро пожаловать!")
    tg("sendMessage", chat_id=chat, text=f"✅ Тема «{g['title']}» создана ({th}) и добавлена в список тем")

# ---------- handlers ----------
HELP = ("Я готовлю черновики постов для @lobbyline.\n/draft — сделать черновик сейчас\n/updates — последние официальные обновления игр\n/memes — мемы на одобрение\n/topics — темы чата\n/votepost — (в General) обновить закреплённую таблицу\n/gamestats — таблица «Какие игры просят» в личку\nЛюбой текст или /ask — вопрос к Gemini, /new — новый диалог\n/allow id|@ник — открыть доступ к боту в личке\n/deny id — закрыть доступ\n/allowed — список допущенных\n/help — помощь\n"
        "Автоматически: 10:00, 15:00, 20:00 (Екб); патчноуты проверяются каждые 30 мин. Ничего не публикуется без кнопки ✅.")
# ---------- game mention tracker (General topic memory) ----------
BOT_ID = [None]
FONT = os.path.join(ASSETS, "fonts", "DejaVuSans.ttf"); FONTB = os.path.join(ASSETS, "fonts", "DejaVuSans-Bold.ttf")
TRACK_EVERY = 1800
def buffer_msg(msg):
    if msg.get("is_topic_message") or msg.get("from", {}).get("is_bot"): return
    txt = (msg.get("text") or msg.get("caption") or "").strip()
    if not txt or txt.startswith("/") or txt.startswith("🗳 Голос за"): return
    with lock:
        s = load_state()
        if msg["chat"]["id"] != s.get("group"): return
        u = msg.get("from", {})
        s.setdefault("chat_buf", []).append({"u": u.get("id"), "n": u.get("first_name", ""), "t": txt[:500], "d": msg.get("date")})
        s["chat_buf"] = s["chat_buf"][-400:]
        save_state(s)

TRACK_PROMPT = ("Ниже сообщения из игрового чата (формат 'user_id: текст'). Найди все упомянутые видеоигры. "
  "Для каждой игры верни каноническое официальное название (как в Steam, на латинице если оно такое), "
  "mentions = сколько РАЗНЫХ пользователей упомянули игру, wants = сколько РАЗНЫХ пользователей просили/хотели "
  "эту игру (тему, поиграть вместе, добавить). Не выдумывай игры. Ответь ТОЛЬКО JSON-массивом вида "
  '[{{"game":"Name","mentions":1,"wants":0}}], пустой массив если игр нет.\n\nСообщения:\n{msgs}')
def track_batch():
    with lock:
        s = load_state(); buf = list(s.get("chat_buf", []))
    if not buf: return False
    txt, _ = gemini_lite(TRACK_PROMPT.format(msgs="\n".join(f"{m['u']}: {m['t']}" for m in buf)))  # raises on 429 -> buffer kept
    j = re.search(r"\[.*\]", txt, re.S); data = json.loads(j.group(0)) if j else []
    today = dt.datetime.now(MSK).strftime("%d.%m.%Y")
    with lock:
        s = load_state(); tab = s.setdefault("game_mentions", {})
        for g in data:
            name = str(g.get("game", "")).strip()
            if not name: continue
            k = _nt(name); e = tab.setdefault(k, {"title": name, "mentions": 0, "wants": 0})
            e["mentions"] += int(g.get("mentions") or 1); e["wants"] += int(g.get("wants") or 0); e["last"] = today
        s["chat_buf"] = s.get("chat_buf", [])[len(buf):]
        s["stats_dirty"] = True; save_state(s)
    log.info("tracker: %d msgs -> %d games", len(buf), len(data)); return True

def stats_rows(s, n=15):
    rows = {}
    for v in s.get("votes", {}).values():
        if not v.get("users"): continue
        k = _nt(v["title"]); r = rows.setdefault(k, {"title": v["title"], "votes": 0, "mentions": 0})
        r["votes"] += len(v["users"]); r["topic"] = r.get("topic") or v.get("status") == "created"
    for k, m in s.get("game_mentions", {}).items():
        r = rows.setdefault(k, {"title": m["title"], "votes": 0, "mentions": 0})
        r["mentions"] += m.get("mentions", 0)
    rows = {k: r for k, r in rows.items() if not r.get("topic") and not has_topic(s, r["title"])}
    for r in rows.values(): r["topic"] = False
    return sorted(rows.values(), key=lambda r: (-(r["votes"] + r["mentions"]), r["title"].lower()))[:n]

INTER = os.path.join(ASSETS, "fonts", "Inter-VariableFont_opsz,wght.ttf")
LOGO = os.path.join(ASSETS, "ll_logo.png"); GLASS = os.path.join(ASSETS, "glass_bar.png")
def _font(size, wght):
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(INTER, size); f.set_variation_by_axes([min(32, max(14, size)), wght]); return f
    except Exception: return ImageFont.truetype(FONTB if wght >= 600 else FONT, size)
def _glass(w, h):
    """Glass bar of width w built from the reference texture: caps kept, middle stretched."""
    from PIL import Image
    src = Image.open(GLASS).convert("RGB"); sw, sh = src.size; k = h / sh
    src = src.resize((max(1, int(sw * k)), h), Image.LANCZOS); sw = src.size[0]; cap = int(sw * 0.06)
    w = max(w, 2 * cap + 1); out = Image.new("RGB", (w, h))
    out.paste(src.crop((0, 0, cap, h)), (0, 0)); out.paste(src.crop((sw - cap, 0, sw, h)), (w - cap, 0))
    out.paste(src.crop((cap, 0, sw - cap, h)).resize((w - 2 * cap, h), Image.LANCZOS), (cap, 0)); return out
def render_stats(s):
    from PIL import Image, ImageDraw, ImageChops
    import io
    rows = stats_rows(s, 10); allsc = sum(r["votes"] + r["mentions"] for r in stats_rows(s, 10**6)) or 1
    W = 1024; n = max(len(rows), 1)
    TOP, RH, BH = (348, 100, 93) if n == 1 else (320, 84, 70)
    H = TOP + RH * n + (248 if n == 1 else 150)
    im = Image.new("RGB", (W, H), (0, 0, 0)); d = ImageDraw.Draw(im)
    rnd = random.Random(42)
    for _ in range(int(W * H / 2600)):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H); v = rnd.randint(40, 140) if rnd.random() < .9 else rnd.randint(150, 230)
        r = 0.6 if v < 150 else 1.0; d.ellipse((x - r, y - r, x + r, y + r), fill=(v, v, min(255, v + 15)))
    try:
        lg = Image.open(LOGO); im.paste(lg, (36, 34), lg)
    except Exception: pass
    d.text((283, 112), "Какие игры просят", font=_font(58, 600), fill=(240, 240, 244))
    d.text((285, 192), "голоса и упоминания в General · Lobby Line", font=_font(25, 400), fill=(140, 140, 148))
    fr, fn, fv = (_font(44, 600), _font(40, 500), _font(24, 500)) if n == 1 else (_font(36, 600), _font(32, 500), _font(22, 500))
    CY = (130, 195, 235)
    if not rows:
        d.text((60, TOP + 20), "Пока данных нет", font=fn, fill=(235, 235, 240))
        d.text((60, TOP + 76), "Нажмите «🔍 Найти игру» или напишите в чат, во что хотите играть".replace("🔍 ", ""), font=fv, fill=(140, 140, 148))
    names = [r["title"] for r in rows]
    nw = max([d.textlength(n, font=fn) for n in names] + [100])
    BX0 = int(min(max(256, 125 + nw + 28), 430)); BX1 = 797
    top = max((rows[0]["votes"] + rows[0]["mentions"]) if rows else 1, 1)
    track = _glass(BX1 - BX0, BH).point(lambda v: int(v * 0.13))
    for i, r in enumerate(rows):
        y = TOP + i * RH; cy = y + BH // 2; v, m = r["votes"], r["mentions"]; sc = v + m; pct = round(100 * sc / allsc)
        rk = str(i + 1); d.text((60 if len(rk) == 1 else 48, cy), rk, font=fr, fill=(245, 245, 248), anchor="lm")
        name = r["title"]
        while d.textlength(name, font=fn) > BX0 - 125 - 30: name = name[:-1]
        if name != r["title"]: name = name.rstrip() + "…"
        d.text((125, cy), name, font=fn, fill=(240, 240, 244), anchor="lm")
        if r["topic"]: d.text((125 + d.textlength(name, font=fn) + 8, cy - 18), "✓", font=_font(20, 600), fill=(120, 225, 170), anchor="lm")
        region = im.crop((BX0, y, BX1, y + BH))
        region = ImageChops.lighter(region, track)
        fw = int((BX1 - BX0) * sc / top)
        if fw > 0:
            g = Image.new("RGB", (BX1 - BX0, BH)); g.paste(_glass(fw, BH), (0, 0)); region = ImageChops.lighter(region, g)
        im.paste(region, (BX0, y)); d = ImageDraw.Draw(im)
        main = f"{pct}% · {v} гол." if m == 0 else f"{pct}% · {sc} всего"
        d.text((816, cy - (10 if m else 0)), main, font=fv, fill=CY, anchor="lm")
        if m: d.text((816, cy + 18), f"{v} гол. + {m} упом." if v else f"{m} упом. в чате", font=_font(16, 400), fill=(120, 120, 130), anchor="lm")
    ff = _font(19, 400)
    d.text((48, H - 70), f"Обновлено {dt.datetime.now(MSK):%d.%m %H:%M} МСК", font=ff, fill=(140, 140, 148))
    leg = "% — доля от всех голосов и упоминаний"; d.text((W - 48 - d.textlength(leg, font=ff), H - 70), leg, font=ff, fill=(140, 140, 148))
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

STATS_MARKUP = {"inline_keyboard": [[{"text": "🔍 Найти игру", "switch_inline_query_current_chat": ""}],
    [{"text": "🏆 Топ", "callback_data": "st:top"}, {"text": "👥 Кто голосовал", "callback_data": "st:who"}],
    [{"text": "⏳ Мой голос", "callback_data": "st:me"}]]}
def vote_rows(s, n=5):
    return sorted([g for g in s.get("votes", {}).values() if g["users"] and g["status"] != "created"], key=lambda g: -len(g["users"]))[:n]
def top_alert(s):
    gs = vote_rows(s)
    if not gs: return "Пока голосов нет — нажмите «🔍 Найти игру»!"
    t = "🏆 Топ-5:\n" + "\n".join(f"{i}. {g['title'][:28]} — {len(g['users'])}" for i, g in enumerate(gs, 1))
    return t[:200]
def voters_text(s):
    gs = vote_rows(s); nm = s.get("names", {})
    if not gs: return "Пока никто не голосовал."
    return "👥 Кто голосовал:\n" + "\n".join(f"• {html.escape(g['title'])} ({len(g['users'])}): " +
        html.escape(", ".join(nm.get(str(u), "участник") for u in list(dict.fromkeys(g["users"]))[:15]) + (" …" if len(g["users"]) > 15 else "")) for g in gs)
# ---- auto-delete ----
def del_later(chat, mid, sec=60, tag=None):
    with lock:
        s = load_state(); q = s.setdefault("autodel", [])
        sp = s.get("statspost") or {}
        if mid == sp.get("mid") and chat == sp.get("chat"): return
        e = next((e for e in q if e["c"] == chat and e["m"] == mid), None)
        if e: e["t"] = max(e["t"], time.time() + sec); e["tag"] = e.get("tag") or tag
        else: q.append({"c": chat, "m": mid, "t": time.time() + sec, "tag": tag, "at": time.time()})
        save_state(s)
def bump_tag(tag, sec=60):
    with lock:
        s = load_state(); ch = False
        for e in s.get("autodel", []):
            if e.get("tag") == tag: e["t"] = time.time() + sec; ch = True
        if ch: save_state(s)
def autodel_loop():
    while True:
        time.sleep(5)
        try:
            with lock:
                s = load_state(); now = time.time(); due = [e for e in s.get("autodel", []) if e["t"] <= now]
                if not due: continue
                s["autodel"] = [e for e in s["autodel"] if e["t"] > now]; save_state(s)
            sp = s.get("statspost") or {}
            for e in due:
                if not (e["m"] == sp.get("mid") and e["c"] == sp.get("chat")):
                    tg("deleteMessage", chat_id=e["c"], message_id=e["m"])
                    log.info("autodel %s after %.0fs", e["m"], time.time() - e.get("at", time.time()))
        except Exception: log.exception("autodel")
def greply(chat, **kw):
    j = tg("sendMessage", chat_id=chat, disable_notification=True, **kw)
    if j.get("ok"): del_later(chat, j["result"]["message_id"])
    return j
GROUP_CMDS = [{"command": "vote", "description": "Найти игру и проголосовать"}, {"command": "top", "description": "Топ игр по голосам"},
              {"command": "voters", "description": "Кто за что голосовал"}, {"command": "stats", "description": "Таблица «Какие игры просят»"},
              {"command": "myvote", "description": "Сколько у меня голосов и когда следующий"}]
def group_cmd(msg, s, cmd):
    chat = msg["chat"]["id"]
    if cmd == "/vote": greply(chat, text="🔍 Нажмите кнопку и начните вводить название игры:", reply_markup={"inline_keyboard": [[{"text": "🔍 Найти игру", "switch_inline_query_current_chat": ""}]]})
    elif cmd == "/top": greply(chat, text=top_alert(s))
    elif cmd == "/voters": greply(chat, text=voters_text(s), parse_mode="HTML")
    elif cmd == "/myvote":
        with lock: s = load_state(); t = myvote_text(s, msg["from"]["id"]); save_state(s)
        nm = msg["from"].get("first_name", "")
        greply(chat, text=f"⏳ {html.escape(nm)}: {t}", parse_mode="HTML")
    elif cmd == "/stats":
        return threading.Thread(target=_stats_reply, args=(chat, s), daemon=True).start()
def _stats_reply(chat, s):
    if True:
        j = tg("sendPhoto", files={"photo": ("gamestats.png", render_stats(s), "image/png")}, chat_id=chat, caption=stats_caption(s), parse_mode="HTML", disable_notification=True)
        if j.get("ok"): del_later(chat, j["result"]["message_id"])
def on_stats_btn(q, s, act):
    if act == "me":
        with lock: s = load_state(); t = myvote_text(s, q["from"]["id"]); save_state(s)
        return tg("answerCallbackQuery", callback_query_id=q["id"], text=t[:200], show_alert=True)
    if act == "top": return tg("answerCallbackQuery", callback_query_id=q["id"], text=top_alert(s), show_alert=True)
    gs = vote_rows(s); nm = s.get("names", {})
    t = "👥 Кто голосовал:\n" + "\n".join(f"{g['title'][:20]}: " + ", ".join(nm.get(str(u), "участник") for u in list(dict.fromkeys(g["users"]))[:4]) +
        (" …" if len(g["users"]) > 4 else "") for g in gs) if gs else "Пока никто не голосовал."
    tg("answerCallbackQuery", callback_query_id=q["id"], text=t[:200], show_alert=True)

def stats_caption(s):
    return ("<b>🎮 Какие игры просят</b>\n"
        "Голосуй за игру, которой не хватает в чате: жми «🔍 Найти игру» (поиск по Steam и Epic).\n"
        "Учитываются голоса и упоминания игр в General.\n"
        "🏆 Топ — пятёрка лидеров · 👥 Кто голосовал — список голосующих\n"
        "У каждого 1 голос, +1 каждые 2 недели (копится до 3) · ⏳ Мой голос\n"
        f"Набралось {VOTE_GOAL} голосов? Откроем для игры отдельную тему.\n"
        "<i>Обновляется раз в 30 минут · меню команд у поля ввода</i>")

def publish_stats(force=False):
    s = load_state(); sp = s.get("statspost"); grp = s.get("group")
    if not grp: return
    png = render_stats(s); cap = stats_caption(s)
    if sp and sp.get("chat") == grp:
        j = tg("editMessageMedia", files={"p": ("gamestats.png", png, "image/png")}, chat_id=grp, message_id=sp["mid"],
               media={"type": "photo", "media": "attach://p", "caption": cap, "parse_mode": "HTML"}, reply_markup=STATS_MARKUP)
        if j.get("ok") or "not modified" in (j.get("description") or ""): ok = True
        else: ok = False; sp = None
    if not sp:
        j = tg("sendPhoto", files={"photo": ("gamestats.png", png, "image/png")}, chat_id=grp, caption=cap, parse_mode="HTML", disable_notification=True, reply_markup=STATS_MARKUP)
        if not j.get("ok"): return
        sp = {"chat": grp, "mid": j["result"]["message_id"]}
        tg("pinChatMessage", chat_id=grp, message_id=sp["mid"], disable_notification=True)
    with lock:
        s = load_state(); s["statspost"] = sp; s["stats_dirty"] = False; s["stats_at"] = time.time(); save_state(s)

def tracker_loop():
    time.sleep(60)
    while True:
        try:
            s = load_state()
            if s.get("chat_buf") and time.time() - s.get("track_at", 0) >= TRACK_EVERY:
                with lock: s = load_state(); s["track_at"] = time.time(); save_state(s)
                run_job("tracker", track_batch, limit=300)
            s = load_state()
            if s.get("stats_dirty") and time.time() - s.get("stats_at", 0) >= TRACK_EVERY: run_job("chart", publish_stats, limit=180)
        except Exception: log.exception("tracker_loop")
        time.sleep(60)

# ---- private-chat whitelist ----
def is_allowed(s, uid): return uid == s.get("owner") or uid in s.get("allowed_users", [])
def note_user(fu):
    if not fu or fu.get("is_bot") or not fu.get("username"): return
    with lock:
        s = load_state(); m = s.setdefault("seen_users", {}); k = fu["username"].lower()
        if m.get(k) != fu["id"]: m[k] = fu["id"]; save_state(s)
def deny_dm(uid):
    with lock:
        s = load_state(); d = s.setdefault("denied_notice", {}); today = dt.date.today().isoformat()
        if d.get(str(uid)) == today: return
        d[str(uid)] = today; save_state(s)
    tg("sendMessage", chat_id=uid, text="Бот закрытый 🔒")
def wl_cmd(msg, s, txt):
    cmd, _, arg = txt.partition(" "); cmd = cmd.split("@")[0]; arg = arg.strip(); chat = msg["chat"]["id"]
    if cmd == "/allowed":
        ids = s.get("allowed_users", []); rev = {v: k for k, v in s.get("seen_users", {}).items()}
        return tg("sendMessage", chat_id=chat, text="Разрешённые пользователи:\n" + ("\n".join(f"• {i}" + (f" (@{rev[i]})" if i in rev else "") for i in ids) or "— пока никого (кроме вас)"))
    uid = None
    rm = msg.get("reply_to_message") or {}
    src = msg.get("forward_origin", {}).get("sender_user") or msg.get("forward_from") or rm.get("forward_origin", {}).get("sender_user") or rm.get("forward_from")
    if src: uid = src["id"]
    elif re.fullmatch(r"-?\d+", arg): uid = int(arg)
    elif arg.startswith("@"): uid = s.get("seen_users", {}).get(arg[1:].lower())
    if not uid:
        return tg("sendMessage", chat_id=chat, text="Не нашёл пользователя. Укажите id, @username (если он писал в группу) или ответьте на пересланное от него сообщение.")
    with lock:
        s = load_state(); al = s.setdefault("allowed_users", [])
        if cmd == "/allow" and uid not in al: al.append(uid)
        if cmd == "/deny" and uid in al: al.remove(uid)
        save_state(s)
    tg("sendMessage", chat_id=chat, text=("✅ Доступ открыт: " if cmd == "/allow" else "🚫 Доступ закрыт: ") + str(uid))

# ---- Gemini chat in private DM (owner + whitelist) ----
CHAT_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash"]   # separate from the lite models used by the batch jobs
CHAT_SYS = ("Ты — помощник Lobby Line в Telegram. Отвечай по-русски (если не просят иначе), по делу и дружелюбно. "
            "Форматирование: только **жирный**, `код` и блоки ```кода```, без таблиц и заголовков #.")
CHAT_HIST = {}          # uid -> [{"role": "user"|"model", "parts": [{"text": ...}]}], last 10 turns, in memory
CHAT_BUSY = {}          # uid -> True while a reply is being generated
CHAT_DOWN = {}          # model -> time until which it's skipped after 429
def _chat_gemini(uid, text):
    hist = CHAT_HIST.setdefault(uid, [])
    contents = hist + [{"role": "user", "parts": [{"text": text}]}]
    last = None
    for m in CHAT_MODELS:
        if CHAT_DOWN.get(m, 0) > time.time(): last = 429; continue
        for attempt in range(2):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                    headers={"x-goog-api-key": GKEY}, timeout=(10, 45 if m == CHAT_MODELS[0] else 90), json={"contents": contents,
                    "systemInstruction": {"parts": [{"text": CHAT_SYS}]}, "generationConfig": {"temperature": 0.7}})
            except requests.RequestException as e: last = type(e).__name__; CHAT_DOWN[m] = time.time() + 120; break
            if r.status_code == 200:
                parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
                ans = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if not ans: last = "empty"; break
                hist += [{"role": "user", "parts": [{"text": text}]}, {"role": "model", "parts": [{"text": ans}]}]
                del hist[:-20]; return ans, m
            last = r.status_code
            if r.status_code == 429: CHAT_DOWN[m] = time.time() + 300; break
            if r.status_code in (500, 503) and attempt == 0: time.sleep(2); continue
            if r.status_code in (500, 503): CHAT_DOWN[m] = time.time() + 120
            break
        log.warning("chat %s -> %s", m, last)
    raise RuntimeError(str(last))
def _md_to_html(t):
    out, parts = [], re.split(r"```(?:[\w+-]*\n)?(.*?)```", t, flags=re.S)
    for i, p in enumerate(parts):
        if i % 2: out.append("<pre>" + html.escape(p.strip("\n")) + "</pre>"); continue
        p = html.escape(p)
        p = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p, flags=re.S)
        p = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", p)
        out.append(p)
    return "".join(out)
def _split(text, n=3500):
    """Split into chunks <= n chars on line breaks, keeping ``` blocks balanced per chunk."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > n: 
            if cur: chunks.append(cur); cur = ""
            chunks.append(line[:n]); line = line[n:]
        if len(cur) + len(line) + 1 > n: chunks.append(cur); cur = ""
        cur += line + "\n"
    if cur.strip(): chunks.append(cur)
    out, carry = [], False
    for ch in chunks:
        if carry: ch = "```\n" + ch
        carry = ch.count("```") % 2 == 1
        out.append(ch + ("```" if carry else ""))
    return [x for x in out if x.strip()]
def _send_long(chat, text):
    for ch in _split(text):
        j = tg("sendMessage", chat_id=chat, text=_md_to_html(ch), parse_mode="HTML", disable_web_page_preview=True)
        if not j.get("ok"): tg("sendMessage", chat_id=chat, text=ch[:4096], disable_web_page_preview=True)
def _chat_reply(chat, text):
    if CHAT_BUSY.get(chat): return tg("sendMessage", chat_id=chat, text="Секунду, ещё думаю над прошлым сообщением ⏳")
    CHAT_BUSY[chat] = True
    stop = threading.Event()
    def typing():
        while not stop.is_set(): tg("sendChatAction", chat_id=chat, action="typing"); stop.wait(4.5)
    threading.Thread(target=typing, daemon=True).start()
    try:
        ans, m = _chat_gemini(chat, text); log.info("chat %s via %s: %d chars", chat, m, len(ans))
        stop.set(); _send_long(chat, ans)
    except Exception as e:
        stop.set(); log.warning("chat failed: %s", e)
        msg = ("Лимит запросов к Gemini на сейчас исчерпан 😔 Попробуй через несколько минут." if "429" in str(e)
               else "Gemini сейчас перегружен или недоступен 😔 Попробуй ещё раз чуть позже.")
        tg("sendMessage", chat_id=chat, text=msg)
    finally: stop.set(); CHAT_BUSY.pop(chat, None)
def _chat_route(chat, txt):
    """Plain text and /ask go to Gemini, /new resets history. Returns True if handled."""
    cmd = txt.split(" ")[0].split("@")[0] if txt.startswith("/") else ""
    if cmd == "/new":
        CHAT_HIST.pop(chat, None); tg("sendMessage", chat_id=chat, text="🆕 Начали новый диалог. Пиши вопрос!"); return True
    if cmd == "/ask":
        q = txt.partition(" ")[2].strip()
        if not q: tg("sendMessage", chat_id=chat, text="Напиши вопрос после команды: /ask как дела?"); return True
    elif cmd or not txt.strip(): return False
    else: q = txt.strip()
    POOL.submit(_safe, "chat", _chat_reply, chat, q); return True

def on_update(u):
    for k in ("message", "callback_query", "inline_query", "chosen_inline_result"):
        if k in u:
            try: note_user(u[k].get("from"))
            except Exception: log.exception("note_user")
    if "chosen_inline_result" in u: return threading.Thread(target=on_chosen, args=(u["chosen_inline_result"],), daemon=True).start()
    if "inline_query" in u: return threading.Thread(target=on_inline, args=(u["inline_query"],), daemon=True).start()
    s = load_state()
    if "message" in u:
        msg = u["message"]; chat = msg["chat"]["id"]; txt = msg.get("text", "")
        if msg["chat"]["type"] != "private":
            if msg["chat"]["type"] == "supergroup":
                handle_group(msg, s)
                t0 = msg.get("text") or ""
                is_card = msg.get("via_bot", {}).get("id") == BOT_ID[0] and t0.startswith("🗳 Голос за")
                # any other message sent through our inline mode (e.g. «🎮 Тема про … уже есть») lives 60 s
                is_notice = msg.get("via_bot", {}).get("id") == BOT_ID[0] and not is_card
                if is_notice: del_later(msg["chat"]["id"], msg["message_id"], 60)
                if is_card:  # stays until its sender votes; safety delete after 60 s
                    del_later(msg["chat"]["id"], msg["message_id"], 60, f"{msg.get('from', {}).get('id')}:{_nt(t0.split(chr(10))[0][len('🗳 Голос за'):])}")
                gc = t0.split()[0].split("@")[0] if t0.startswith("/") else ""
                if gc in ("/vote", "/top", "/voters", "/stats", "/myvote") and msg["chat"]["id"] == s.get("group"):
                    group_cmd(msg, load_state(), gc)
                try: buffer_msg(msg)
                except Exception: log.exception("buffer")
                # keep General clean: everything except the pinned table is removed at once (inline vote cards too: the vote is counted on chosen_inline_result)
                s2 = load_state(); sp = s2.get("statspost") or {}
                if msg["chat"]["id"] == s2.get("group") and not msg.get("is_topic_message") and msg["message_id"] != sp.get("mid") and not is_card and not is_notice:
                    tg("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
            return
        if s["owner"] is not None and not is_allowed(s, chat): return deny_dm(chat)
        if chat == s["owner"] and txt.split(" ")[0].split("@")[0] in ("/allow", "/deny", "/allowed"): return wl_cmd(msg, s, txt)
        if s["owner"] is not None and _chat_route(chat, txt): return
        if chat != s["owner"] and s["owner"] is not None:
            return tg("sendMessage", chat_id=chat, text="Привет! Доступ открыт ✅ Пиши мне любой вопрос — отвечу через Gemini. /new — новый диалог.\nГолосовать за игры можно в «Лобби Line Чат» через «🔍 Найти игру».".replace("Лобби", "Lobby"))
        if s["owner"] is None and txt.startswith("/start"):
            s["owner"] = chat; save_state(s); log.info("owner registered")
            tg("sendMessage", chat_id=chat, text="Вы зарегистрированы как владелец 👑\n" + HELP); return
        if chat != s["owner"]: return
        if txt.startswith("/draft"):
            tg("sendMessage", chat_id=chat, text="Ищу новости и пишу черновик… ⏳")
            threading.Thread(target=run_cycle, kwargs={"chat": chat, "n": 1}, daemon=True).start()
        elif txt.startswith("/updates"):
            threading.Thread(target=lambda: tg("sendMessage", chat_id=chat, text=latest_updates_text(), parse_mode="HTML",
                             disable_web_page_preview=True), daemon=True).start()
        elif txt.startswith("/memes"):
            tg("sendMessage", chat_id=chat, text="Собираю мемы… ⏳")
            threading.Thread(target=meme_batch, kwargs={"chat": chat}, daemon=True).start()
        elif txt.startswith("/topics"): topics_cmd(chat, txt)
        elif txt.startswith("/gamestats"):
            def _gs():
                tg("sendPhoto", files={"photo": ("gamestats.png", render_stats(load_state()), "image/png")}, chat_id=chat,
                   caption=stats_caption(load_state()), parse_mode="HTML")
            threading.Thread(target=_gs, daemon=True).start()
        elif txt.startswith(("/help", "/start")): tg("sendMessage", chat_id=chat, text=HELP)
    elif "my_chat_member" in u:
        c = u["my_chat_member"]["chat"]
        if c.get("type") == "supergroup" and (c.get("is_forum") or not s.get("group")):
            s["group"] = c["id"]; s["group_title"] = c.get("title"); save_state(s); log.info("group via my_chat_member %s", c["id"])
    elif "callback_query" in u:
        q = u["callback_query"]; chat = q["from"]["id"]
        if q.get("message", {}).get("chat", {}).get("type") == "private" and not is_allowed(s, chat):
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Бот закрытый 🔒", show_alert=True); return
        if (q.get("data") or "").startswith("st:"):
            return on_stats_btn(q, load_state(), q["data"][3:])
        if (q.get("data") or "").startswith("v:"):
            with lock: s = load_state(); return on_vote(q, s, q["data"][2:])
        if chat != s["owner"]: return tg("answerCallbackQuery", callback_query_id=q["id"])
        act, did = q["data"].split(":", 1)
        if act in ("vt", "vx"): return on_vote_owner(q, s, act, did)
        if act in ("mok", "mno"):
            m = s.get("memes", {}).get(did); mid = q["message"]["message_id"]
            if not m or m["status"] != "pending": return tg("answerCallbackQuery", callback_query_id=q["id"], text="Уже обработано")
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
            if act == "mno": m["status"] = "rejected"; note = "❌ Пропущено"
            else:
                th, grp = topic_of(s, m["game"]), s.get("group")
                if th and grp and post_meme(grp, th, m): m["status"] = "posted"; note = f"✅ Опубликовано в тему {m['game']}"
                else: m["status"] = "queued"; note = f"⏳ В очереди: тема {m['game']} ещё не привязана"
            tg("answerCallbackQuery", callback_query_id=q["id"], text=note); save_state(s); return
        d = s["drafts"].get(did)
        if not d or d["status"] != "pending":
            return tg("answerCallbackQuery", callback_query_id=q["id"], text="Черновик уже обработан")
        mid = q["message"]["message_id"]
        if act in ("pub", "chat"):
            ok = True
            if act == "pub": ok = send_post(CHANNEL, dict(d))
            note = "✅ Опубликовано в @lobbyline" if act == "pub" else ""
            if ok:
                th, grp = topic_of(s, d["item"]["game"]), s.get("group")
                if th and grp and send_post(grp, dict(d), extra={"message_thread_id": th}):
                    d["chat_status"] = "posted"; note += f"\n💬 Отправлено в тему {d['item']['game']}"
                else: d["chat_status"] = "queued"; note += f"\n⏳ В чат уйдёт, когда будет привязана тема {d['item']['game']}"
            d["status"] = "published" if ok else "pending"
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Готово ✅" if ok else "Ошибка публикации")
            if ok: tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []}); tg("sendMessage", chat_id=chat, text=note.strip())
        elif act == "no":
            d["status"] = "rejected"; tg("answerCallbackQuery", callback_query_id=q["id"], text="Отклонено")
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        elif act == "re":
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Переписываю… ✏️")
            try:
                d["text"], d["model"] = write_post(d["item"], " Напиши заново, другими словами и с другим заходом, чем раньше.")
                d["status"] = "rejected"; nd = dict(d, id=did[:6] + hashlib.md5(str(time.time()).encode()).hexdigest()[:6], status="pending")
                s["drafts"][nd["id"]] = nd
                tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
                save_state(s); draft_to_owner(nd); return
            except Exception as e: tg("sendMessage", chat_id=chat, text=f"Не вышло переписать: {e}")
        save_state(s)

def scheduler():
    done = set()
    while True:
        now = dt.datetime.now(TZ)
        for h, m in SCHEDULE:
            key = f"{now.date()}-{h}"
            if now.hour == h and now.minute >= m and key not in done:
                done.add(key); log.info("scheduled run %s", key)
                run_job("news", run_cycle)
        time.sleep(30)

def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] == "memetest":
        seen = set(load_state().get("meme_seen", []))
        for g in GAMES:
            m = fetch_meme(g, set(seen)); print(g, "->", (m["src"], m["author"], m["title"][:60], m["url"], m["vision"]) if m else None, flush=True)
        print("sources:", SRC_STATUS); return
    if len(sys.argv) > 1 and sys.argv[1] == "sample":
        items = gather(5); it = next((i for i in items if i["images"] or i["videos"]), items[0] if items else None)
        d = make_draft(it); out = os.path.join(DATA, "sample_draft.txt")
        open(out, "w").write(f"MODEL: {d['model']}\nGAME: {it['game']}\nSOURCE: {it['url']}\nMEDIA: " +
            json.dumps([{k: m[k] for k in ('type', 'url', 'path')} | {'bytes': os.path.getsize(m['path'])} for m in d['media']], ensure_ascii=False, indent=1) +
            f"\nYOUTUBE: {it['youtube']}\n\n{d['text']}\n"); print(out); return
    tg("setMyCommands", scope={"type": "all_private_chats"}, commands=[{"command": "ask", "description": "Спросить Gemini"},
        {"command": "new", "description": "Новый диалог с Gemini (сбросить контекст)"}, {"command": "help", "description": "Помощь"}])
    tg("setMyCommands", commands=[{"command": "draft", "description": "Сделать черновик сейчас"}, {"command": "updates", "description": "Последние обновления игр"}, {"command": "memes", "description": "Мемы на одобрение сейчас"}, {"command": "topics", "description": "Темы чата"}, {"command": "gamestats", "description": "Таблица: какие игры просят"}, {"command": "help", "description": "Помощь"}])
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=meme_loop, daemon=True).start()
    threading.Thread(target=votepost_loop, daemon=True).start()
    threading.Thread(target=tracker_loop, daemon=True).start()
    threading.Thread(target=autodel_loop, daemon=True).start()
    with lock:
        s0 = load_state(); migrate_wallets(s0); existing_topics(s0); rf = refund_topic_votes(s0); save_state(s0)
    log.info("topic-vote audit: refunded %s; topics without thread id: %s", rf or "nothing",
             [k for k, e in s0.get("existing_topics", {}).items() if not e.get("th")])
    BOT_ID[0] = tg("getMe").get("result", {}).get("id")
    g0 = load_state().get("group")
    if g0: tg("setMyCommands", commands=GROUP_CMDS, scope={"type": "chat", "chat_id": g0})
    threading.Thread(target=watchdog, daemon=True).start()
    def _gemini_selftest():
        try: t, m = gemini_lite("Ответь одним словом: работает?"); log.info("gemini self-test ok: %s -> %s", m, t[:40])
        except Exception as e: log.warning("gemini self-test failed: %s", e)
    threading.Thread(target=_gemini_selftest, daemon=True).start()
    log.info("bot started"); offset = None
    PST = {"n": 0, "max": 0.0, "sum": 0.0, "t": time.time()}
    AU = json.dumps(["message", "callback_query", "my_chat_member", "inline_query", "chosen_inline_result"])
    while True:
        t0 = time.time()
        try:
            j = requests.post(API + "getUpdates", data={"timeout": 20, "offset": offset or 0, "allowed_updates": AU}, timeout=(10, 30)).json()
        except Exception as e:
            log.warning("poll failed after %.1fs: %s", time.time() - t0, type(e).__name__); time.sleep(2); continue
        dtp = time.time() - t0
        if not j.get("ok"): log.warning("poll not ok: %s", j.get("description")); time.sleep(3); continue
        try: open(HEARTBEAT, "w").write(str(int(time.time())))  # only after a successful getUpdates round-trip
        except OSError: pass
        res = j.get("result", [])
        PST["n"] += 1; PST["max"] = max(PST["max"], dtp); PST["sum"] += dtp
        if time.time() - PST["t"] > 600:
            log.info("poll stats 10m: %d polls, avg %.1fs, max %.1fs", PST["n"], PST["sum"] / max(PST["n"], 1), PST["max"])
            PST.update(n=0, max=0.0, sum=0.0, t=time.time())
        if dtp > 25: log.warning("poll slow: %.1fs, %d updates", dtp, len(res))
        for u in res:
            offset = u["update_id"] + 1
            obj = next((u[k] for k in ("message", "callback_query", "inline_query", "chosen_inline_result", "my_chat_member") if k in u), {})
            ud = obj.get("date") or (obj.get("message") or {}).get("date")
            kind = next((k for k in u if k != "update_id"), "?")
            if ud: (log.warning if time.time() - ud > 10 else log.info)("update %s %s lag %.1fs (poll %.1fs)", u["update_id"], kind, time.time() - ud, dtp)
            else: log.info("update %s %s (poll %.1fs)", u["update_id"], kind, dtp)
            try:
                if kind == "callback_query": POOL.submit(_safe, "callback", on_update, u)  # never wait behind another handler
                else: on_update(u)
            except Exception: log.exception("update")

if __name__ == "__main__": main()
