import os, io, json, time, hashlib, contextlib, urllib.parse, httpx
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()
PROFIL_FILE = "data/profil.json"
CHROMA_DIR = "data/chroma"
TV_IP = os.getenv("TV_IP", "192.168.1.50")
TV_PORT = 5555
ADB_KEY = "data/adbkey"
ALLOW_EXEC = os.getenv("JARVIS_ALLOW_EXEC") == "1"
_client = httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "NOVA-OS/1.0"})

# ---------- IA (avec secours automatique) ----------
_bad = {}

def _providers():
    out = []
    k = os.getenv("GROQ_API_KEY")
    if k:
        for m in ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]:
            out.append({"url": "https://api.groq.com/openai/v1/chat/completions", "key": k, "model": m})
    g = os.getenv("GEMINI_API_KEY")
    if g:
        out.append({"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "key": g,
                    "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash")})
    return out

def chat_once(messages, tools):
    last = None
    for p in _providers():
        if _bad.get(p["model"], 0) > time.time():
            continue
        body = {"model": p["model"], "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.4}
        if "gpt-oss" in p["model"]:
            body["reasoning_effort"] = "low"
        try:
            r = _client.post(p["url"], headers={"Authorization": f"Bearer {p['key']}"}, json=body)
            if r.status_code >= 400:
                last = f"{p['model']} {r.status_code}: {r.text[:150]}"
                print("⚠️", last)
                _bad[p["model"]] = time.time() + (30 if r.status_code == 429 else 120)
                continue
            return r.json()["choices"][0]["message"]
        except Exception as e:
            last = f"{p['model']}: {e}"
            print("⚠️", last)
            _bad[p["model"]] = time.time() + 30
    raise RuntimeError(last or "Aucun modèle configuré")

# ---------- Mémoire ----------
_collection = None
def _get_collection():
    global _collection
    if _collection is not None: return _collection
    try:
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
        col = client.get_or_create_collection("faits")
        if col.count() == 0 and os.path.exists(PROFIL_FILE):
            try:
                with open(PROFIL_FILE, "r", encoding="utf-8") as f:
                    for fait in json.load(f).get("faits", []): col.upsert(ids=[_fact_id(fait)], documents=[fait])
            except Exception: pass
        _collection = col
    except Exception as e:
        print(f"⚠️ ChromaDB indisponible : {e}")
        _collection = False
    return _collection

def _fact_id(fait: str) -> str:
    return hashlib.sha1(fait.strip().lower().encode("utf-8")).hexdigest()

def save_fact(fait: str) -> bool:
    col = _get_collection()
    if not col or not fait.strip(): return False
    col.upsert(ids=[_fact_id(fait)], documents=[fait.strip()], metadatas=[{"date": time.strftime("%Y-%m-%d")}])
    return True

def search_facts(question: str, k: int = 8) -> list:
    col = _get_collection()
    if not col: return []
    n = col.count()
    if n == 0: return []
    res = col.query(query_texts=[question], n_results=min(k, n))
    return res["documents"][0] if res.get("documents") else []

# ---------- TV ----------
def controler_google_tv(action: str, app_name: str = "") -> str:
    try:
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner
    except ImportError: return "Erreur : installe adb-shell."

    action = (action or "").lower().strip()
    if action == "lancer_app":
        pkg = {"netflix": "com.netflix.ninja", "youtube": "com.google.android.youtube.tv", "disney": "com.disney.disneyplus", "prime video": "com.amazon.amazonvideo.livingroom", "spotify": "com.spotify.tv.android"}.get((app_name or "").lower().strip())
        if not pkg: return "Application inconnue."
        cmd = f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
    else:
        keys = {"allumer": "KEYCODE_WAKEUP", "eteindre": "KEYCODE_SLEEP", "accueil": "KEYCODE_HOME", "retour": "KEYCODE_BACK", "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "volume_plus": "KEYCODE_VOLUME_UP", "volume_moins": "KEYCODE_VOLUME_DOWN", "muet": "KEYCODE_VOLUME_MUTE"}
        if action in keys: cmd = f"input keyevent {keys[action]}"
        else: return "Action inconnue."

    try:
        os.makedirs(os.path.dirname(ADB_KEY), exist_ok=True)
        if not os.path.exists(ADB_KEY): keygen(ADB_KEY)
        with open(ADB_KEY) as f: priv = f.read()
        with open(ADB_KEY + ".pub") as f: pub = f.read()
        dev = AdbDeviceTcp(TV_IP, TV_PORT, default_transport_timeout_s=9.0)
        dev.connect(rsa_keys=[PythonRSASigner(pub, priv)], auth_timeout_s=15)
        dev.shell(cmd)
        return f"Commande envoyée à la TV : {action} {app_name}".strip()
    except Exception as e: return f"Erreur TV ({TV_IP}) : {e}"

# ---------- Météo (Open-Meteo, cache 10 min) ----------
_wcache = {}
WMO = {0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert", 45: "brouillard", 48: "brouillard givrant",
       51: "bruine légère", 53: "bruine", 55: "bruine forte", 56: "bruine verglaçante", 57: "bruine verglaçante",
       61: "pluie légère", 63: "pluie", 65: "forte pluie", 66: "pluie verglaçante", 67: "pluie verglaçante",
       71: "neige légère", 73: "neige", 75: "forte neige", 77: "grains de neige",
       80: "averses légères", 81: "averses", 82: "fortes averses", 85: "averses de neige", 86: "fortes averses de neige",
       95: "orage", 96: "orage avec grêle", 99: "orage violent avec grêle"}

def _wmo(c): return WMO.get(c, "temps variable")

def _icon(c):
    if c in (0, 1): return "☀️"
    if c == 2: return "⛅"
    if c == 3: return "☁️"
    if c in (45, 48): return "🌫️"
    if c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82): return "🌧️"
    if c in (71, 73, 75, 77, 85, 86): return "❄️"
    if c >= 95: return "⛈️"
    return "🌡️"

def _weather_data(city: str):
    city = (city or "Bruxelles").strip()
    key = city.lower()
    hit = _wcache.get(key)
    if hit and time.time() - hit[0] < 600: return hit[1]
    g = _client.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "fr"}).json()["results"][0]
    w = _client.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,uv_index_max,sunrise,sunset"}).json()
    data = {"city": g["name"], "w": w}
    _wcache[key] = (time.time(), data)
    return data

def _day(dd, i):
    return (f"{dd['time'][i]} : {round(dd['temperature_2m_min'][i])}→{round(dd['temperature_2m_max'][i])}°C, {_wmo(dd['weather_code'][i])}, "
            f"pluie {dd['precipitation_probability_max'][i] or 0}% ({dd['precipitation_sum'][i] or 0} mm), "
            f"vent max {round(dd['wind_speed_10m_max'][i] or 0)} km/h, UV {round(dd['uv_index_max'][i] or 0)}, "
            f"soleil {dd['sunrise'][i][-5:]}–{dd['sunset'][i][-5:]}")

def get_weather(city: str, jour: int = 0) -> str:
    try:
        d = _weather_data(city); w = d["w"]; dd = w["daily"]; n = d["city"]
        jour = max(0, min(int(jour), len(dd["time"]) - 1))
        out = []
        if jour == 0:
            c = w["current"]
            out.append(f"Maintenant à {n} : {round(c['temperature_2m'])}°C (ressenti {round(c['apparent_temperature'])}°C), {_wmo(c['weather_code'])}, humidité {c['relative_humidity_2m']}%, vent {round(c['wind_speed_10m'])} km/h.")
        out.append(f"Jour demandé à {n} — {_day(dd, jour)}")
        suite = [_day(dd, i) for i in range(jour + 1, min(jour + 4, len(dd["time"])))]
        if suite: out.append("Jours suivants :\n" + "\n".join(suite))
        return "\n".join(out)
    except Exception:
        return f"Impossible de récupérer la météo pour {city}."

def weather_header(city: str = "Bruxelles"):
    try:
        d = _weather_data(city); c = d["w"]["current"]
        return {"temp": round(c["temperature_2m"]), "desc": _wmo(c["weather_code"]), "icon": _icon(c["weather_code"]), "city": d["city"]}
    except Exception:
        return None

# ---------- Autres sources de données ----------
def wiki(q: str) -> str:
    s = _client.get("https://fr.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 1}).json()["query"]["search"]
    if not s: return "Aucun résultat."
    t = s[0]["title"]
    r = _client.get("https://fr.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(t.replace(" ", "_"))).json()
    return f"{t} : {(r.get('extract') or '')[:1200]}"

def get_news(sujet: str = "") -> str:
    if sujet:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": sujet, "hl": "fr", "gl": "BE", "ceid": "BE:fr"})
    else:
        url = "https://news.google.com/rss?hl=fr&gl=BE&ceid=BE:fr"
    root = ET.fromstring(_client.get(url).content)
    items = root.findall(".//item")[:6]
    return "\n".join(f"- {i.findtext('title')} ({(i.findtext('pubDate') or '')[:16]})" for i in items) or "Aucune actualité."

def convert(montant: float, de: str, vers: str) -> str:
    r = _client.get(f"https://open.er-api.com/v6/latest/{de.upper()}").json()
    rate = r["rates"][vers.upper()]
    return f"{montant} {de.upper()} = {round(montant * rate, 2)} {vers.upper()} (taux {rate:.4f})"

# ---------- Outils ----------
def _t(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": req}}}

def build_tools():
    S = {"type": "string"}
    tools = [
        _t("donner_meteo", "Météo actuelle ou prévision jusqu'à 7 jours (vent, UV, pluie, soleil).",
           {"ville": S, "jour": {"type": "integer", "description": "0 aujourd'hui, 1 demain, 2 après-demain..."}}, ["ville"]),
        _t("rechercher_sur_wikipedia", "Résumé Wikipédia sur un sujet.", {"requete": S}, ["requete"]),
        _t("actualites", "Dernières actualités (sujet optionnel).", {"sujet": S}, []),
        _t("convertir_devise", "Convertit un montant entre deux devises (codes ISO).",
           {"montant": {"type": "number"}, "de": S, "vers": S}, ["montant", "de", "vers"]),
        _t("memoriser_fait", "Sauvegarde un fait durable sur Tarik.", {"fait": S}, ["fait"]),
        _t("controler_google_tv", "Contrôle la TV : allumer, eteindre, accueil, retour, play_pause, volume_plus, volume_moins, muet, lancer_app (+ app_name).",
           {"action": S, "app_name": S}, ["action"]),
    ]
    if ALLOW_EXEC:
        tools.append(_t("executer_python", "Exécute du code Python.", {"code": S}, ["code"]))
    return tools

def run_tool(name: str, args: dict) -> str:
    try:
        if name == "executer_python":
            if not ALLOW_EXEC: return "Désactivé."
            out = io.StringIO()
            with contextlib.redirect_stdout(out): exec(args.get("code", ""), {})
            return out.getvalue() or "Succès"
        if name == "rechercher_sur_wikipedia": return wiki(args.get("requete", ""))
        if name == "actualites": return get_news(args.get("sujet", ""))
        if name == "convertir_devise": return convert(float(args.get("montant", 1)), args.get("de", "EUR"), args.get("vers", "USD"))
        if name == "memoriser_fait": return "Mémorisé." if save_fact(args.get("fait", "")) else "Erreur."
        if name == "controler_google_tv": return controler_google_tv(args.get("action", ""), args.get("app_name", ""))
        if name == "donner_meteo": return get_weather(args.get("ville") or "Bruxelles", int(args.get("jour", 0) or 0))
        return f"Outil inconnu : {name}"
    except Exception as e:
        return f"Erreur outil {name} : {e}"

# ---------- Cerveau ----------
class GroqAdapter:
    @staticmethod
    def call(provider: dict, task: str, history: list = None):
        history = history or []
        faits = search_facts(task, k=8)
        memoire = "\n".join(f"- {f}" for f in faits) if faits else "Aucun fait pertinent."
        jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        now = datetime.now(ZoneInfo("Europe/Brussels"))
        date = f"{jours[now.weekday()]} {now.strftime('%d/%m/%Y, %H:%M')} (Bruxelles)"

        system_prompt = f"""Tu es A.R.I.A. (Assistant de Réponse et d'Intelligence Autonome), l'assistante personnelle de Tarik, dans le système NOVA OS.
Personnalité : futuriste, élégante, vive, une pointe d'humour. Tu TUTOIES Tarik, tu réponds toujours en français, en Markdown léger.

Date et heure actuelles : {date}.

Style : réponds d'abord précisément à la question, puis ajoute 1 à 3 infos bonus utiles qu'il n'a pas demandées
(ex. météo : pluie, vent, UV, lever/coucher du soleil, quoi porter ; actu : contexte ; question factuelle : un détail marquant). Pas de blabla.

Règles :
- N'écris JAMAIS « je n'ai pas cette donnée » sans avoir essayé un outil.
- Météo : 'donner_meteo' (jour=0 aujourd'hui, 1 demain, 2 après-demain...). Ville par défaut : Bruxelles.
- Actualité : 'actualites'. Faits, personnes, lieux : 'rechercher_sur_wikipedia'. Devises : 'convertir_devise'.
- Dès que Tarik donne une info durable (goût, habitude, projet, personne), appelle 'memoriser_fait' sans demander.
- TV : 'controler_google_tv'. Calcul précis : 'executer_python' s'il est disponible.
- Plusieurs outils utiles ? Appelle-les ensemble.
- Si tu ne sais pas, dis-le : n'invente jamais. Sur Tarik, base-toi uniquement sur les faits mémorisés.

Faits mémorisés :
{memoire}
"""
        messages = [{"role": "system", "content": system_prompt}] + history[-10:] + [{"role": "user", "content": task}]
        tools = build_tools()

        def _exec(tc):
            try: args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception: args = {}
            return {"role": "tool", "tool_call_id": tc["id"], "content": run_tool(tc["function"]["name"], args)}

        try:
            for _ in range(5):
                msg = chat_once(messages, tools)
                calls = msg.get("tool_calls")
                if not calls:
                    return {"status": "success", "message": msg.get("content") or ""}
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                with ThreadPoolExecutor(max_workers=4) as pool:
                    messages.extend(pool.map(_exec, calls))
            return {"status": "success", "message": "Je tourne en rond sur celle-là, reformule-moi ça."}
        except Exception as e:
            print("❌ Erreur cerveau :", e)
            return {"status": "error", "message": "Petite panne technique, reformule."}
