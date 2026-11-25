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
    mk = os.getenv("MISTRAL_API_KEY")
    if mk:
        out.append({"url": "https://api.mistral.ai/v1/chat/completions", "key": mk,
                    "model": os.getenv("MISTRAL_MODEL", "mistral-large-latest")})
    k = os.getenv("GROQ_API_KEY")
    if k:
        for m in ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]:
            out.append({"url": "https://api.groq.com/openai/v1/chat/completions", "key": k, "model": m})
    g = os.getenv("GEMINI_API_KEY")
    if g:
        out.append({"url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "key": g,
                    "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash")})
    return out

def chat_once(messages, tools, effort="low"):
    last = None
    for p in _providers():
        if _bad.get(p["model"], 0) > time.time():
            continue
        body = {"model": p["model"], "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.7}
        if "gpt-oss" in p["model"]:
            body["reasoning_effort"] = effort
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

# ---------- Agenda, favoris, préférences, domotique ----------
from datetime import timedelta
AGENDA_FILE, FAVORIS_FILE, PREFS_FILE = "data/agenda.json", "data/favoris.json", "data/prefs.json"

def _jload(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def _jsave(path, data):
    os.makedirs("data", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)

def _now(): return datetime.now(ZoneInfo("Europe/Brussels")).replace(tzinfo=None)

def agenda_add(titre, debut, note=""):
    try: d = datetime.fromisoformat(debut).replace(tzinfo=None)
    except Exception: return "Date invalide : utilise le format AAAA-MM-JJTHH:MM."
    ev = _jload(AGENDA_FILE, [])
    ev.append({"id": hashlib.sha1(f"{titre}{debut}{time.time()}".encode()).hexdigest()[:6], "titre": titre.strip(),
               "debut": d.strftime("%Y-%m-%dT%H:%M"), "note": note})
    _jsave(AGENDA_FILE, ev)
    return f"Ajouté à l'agenda : {titre} le {d.strftime('%d/%m/%Y à %H:%M')}."

def agenda_list(jours=14):
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(1, int(jours)))
    ev = []
    for e in _jload(AGENDA_FILE, []):
        try:
            if start <= datetime.fromisoformat(e["debut"]) <= end: ev.append(e)
        except Exception: pass
    ev.sort(key=lambda e: e["debut"])
    return "\n".join(f"- [{e['id']}] {e['debut'].replace('T', ' ')} : {e['titre']}" + (f" ({e['note']})" if e.get("note") else "") for e in ev) or "Rien de prévu."

def agenda_del(ident):
    ev = _jload(AGENDA_FILE, [])
    reste = [e for e in ev if e.get("id") != ident]
    if len(reste) == len(ev): return "Aucun événement avec cet id."
    _jsave(AGENDA_FILE, reste)
    return "Événement supprimé."

def fav_add(nom, categorie, note=""):
    fav = _jload(FAVORIS_FILE, [])
    fav = [f for f in fav if f["nom"].lower() != nom.strip().lower()]
    fav.append({"nom": nom.strip(), "categorie": categorie.strip().lower(), "note": note})
    _jsave(FAVORIS_FILE, fav)
    return f"Favori ajouté : {nom} ({categorie})."

def fav_list(categorie=""):
    c = (categorie or "").strip().lower()
    fav = [f for f in _jload(FAVORIS_FILE, []) if not c or c in f["categorie"]]
    return "\n".join(f"- {f['nom']} [{f['categorie']}]" + (f" : {f['note']}" if f.get("note") else "") for f in fav) or "Aucun favori enregistré."

def pref_set(cle, valeur):
    p = _jload(PREFS_FILE, {})
    p[cle.strip().lower()] = valeur.strip()
    _jsave(PREFS_FILE, p)
    return f"Préférence enregistrée : {cle} = {valeur}."

def _context_block():
    prefs = _jload(PREFS_FILE, {})
    out = ""
    if prefs: out += "Préférences de Tarik (à respecter toujours) :\n" + "\n".join(f"- {k} : {v}" for k, v in prefs.items()) + "\n\n"
    ag = agenda_list(3)
    if ag != "Rien de prévu.": out += "Agenda des 3 prochains jours :\n" + ag + "\n\n"
    return out

def domotique(action, appareil="", valeur=None, pin=""):
    url, tok = os.getenv("HA_URL", "").rstrip("/"), os.getenv("HA_TOKEN", "")
    if not url or not tok: return "Domotique non configurée (HA_URL et HA_TOKEN manquants dans .env)."
    h = {"Authorization": f"Bearer {tok}"}
    doms = ("light", "switch", "climate", "cover", "lock", "scene", "script", "camera", "fan", "media_player")
    devs = [d for d in _client.get(url + "/api/states", headers=h).json() if d["entity_id"].split(".")[0] in doms]
    name = lambda d: d["attributes"].get("friendly_name", d["entity_id"])
    if action == "lister":
        return "\n".join(f"- {name(d)} ({d['entity_id'].split('.')[0]}) : {d['state']}" for d in devs[:60]) or "Aucun appareil."
    a = (appareil or "").lower().strip()
    cible = [d for d in devs if a and (a in name(d).lower() or a == d["entity_id"])]
    exact = [d for d in cible if name(d).lower() == a]
    if not cible: return "Appareil introuvable. Utilise l'action 'lister'."
    if len(cible) > 1 and not exact:
        return "Plusieurs appareils correspondent : " + ", ".join(name(d) for d in cible[:6]) + ". Précise lequel."
    d = (exact or cible)[0]; eid = d["entity_id"]; dom = eid.split(".")[0]
    data = {"entity_id": eid}
    if action == "etat":
        at = d["attributes"]
        keep = {k: at[k] for k in ("current_temperature", "temperature", "hvac_mode", "battery_level", "current_position", "brightness") if k in at}
        return f"{name(d)} : {d['state']} {keep}"
    if dom == "lock":
        if action == "verrouiller": svc = "lock"
        elif action == "deverrouiller":
            ok = os.getenv("HA_PIN", "")
            if not ok or str(pin) != ok: return "Déverrouillage refusé : le code PIN est requis."
            svc = "unlock"
        else: return "Pour une serrure : verrouiller ou deverrouiller."
    elif action == "regler":
        if dom == "climate": svc = "set_temperature"; data["temperature"] = float(valeur)
        elif dom == "light": svc = "turn_on"; data["brightness_pct"] = int(valeur)
        elif dom == "cover": svc = "set_cover_position"; data["position"] = int(valeur)
        else: return "Réglage impossible pour cet appareil."
    elif action in ("ouvrir", "fermer"):
        if dom != "cover": return "Cette action ne concerne que les volets et portes motorisées."
        svc = "open_cover" if action == "ouvrir" else "close_cover"
    else:
        svc = {"allumer": "turn_on", "eteindre": "turn_off", "basculer": "toggle", "activer": "turn_on"}.get(action)
        if not svc: return "Action inconnue."
    r = _client.post(f"{url}/api/services/{dom}/{svc}", headers=h, json=data)
    return f"{name(d)} : {action} effectué." if r.status_code < 300 else f"Échec ({r.status_code})."

# ---------- Internet ----------
import ipaddress, socket
from html.parser import HTMLParser

def _safe_url(u):
    pr = urllib.parse.urlparse(u)
    if pr.scheme not in ("http", "https") or not pr.hostname: return False
    try:
        for i in socket.getaddrinfo(pr.hostname, None):
            ip = ipaddress.ip_address(i[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: return False
    except Exception: return False
    return True

class _Txt(HTMLParser):
    SKIP = ("script", "style", "nav", "footer", "header", "svg", "noscript")
    def __init__(self):
        super().__init__(); self.out = []; self.skip = 0
    def handle_starttag(self, t, a):
        if t in self.SKIP: self.skip += 1
    def handle_endtag(self, t):
        if t in self.SKIP and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip and d.strip(): self.out.append(d.strip())

def lire_page(url):
    cur, r = url, None
    for _ in range(4):
        if not _safe_url(cur): return "Adresse non autorisée."
        r = _client.get(cur, follow_redirects=False, timeout=15)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            cur = urllib.parse.urljoin(cur, r.headers["location"]); continue
        break
    if r is None or r.status_code >= 400: return "Page inaccessible."
    ct = r.headers.get("content-type", "")
    if "html" not in ct and "text" not in ct: return "Contenu non lisible."
    p = _Txt(); p.feed(r.text[:400000])
    return " ".join(p.out)[:5000] or "Page vide."

def recherche_web(q, periode=""):
    tk = os.getenv("TAVILY_API_KEY")
    if tk:
        try:
            r = _client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {tk}"},
                             json={"query": q, "max_results": 6, "include_answer": True}).json()
            out = []
            if r.get("answer"): out.append("Synthèse : " + r["answer"])
            out += [f"- {x['title']} ({x['url']}) : {x['content'][:350]}" for x in r.get("results", [])]
            if out: return "\n".join(out)
        except Exception as e:
            print("⚠️ Tavily :", e)
    try:
        try: from ddgs import DDGS
        except ImportError: from duckduckgo_search import DDGS
        tl = {"jour": "d", "semaine": "w", "mois": "m", "annee": "y"}.get((periode or "").lower())
        res = DDGS().text(q, region="fr-fr", max_results=6, timelimit=tl)
        return "\n".join(f"- {x['title']} ({x['href']}) : {x['body'][:350]}" for x in res) or "Aucun résultat."
    except Exception as e:
        return f"Recherche impossible : {e}"

# ---------- Jarvis : audit, alertes, scénarios, habitudes, trajets ----------
SCEN_FILE, HAB_FILE = "data/scenarios.json", "data/habitudes.json"
_alerted = set()
JOURS_C = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

def _audit(name, args):
    try:
        safe = {k: str(v)[:80] for k, v in (args or {}).items() if k not in ("code", "pin")}
        os.makedirs("data", exist_ok=True)
        with open("data/audit.log", "a", encoding="utf-8") as f:
            f.write(f"{_now().strftime('%Y-%m-%d %H:%M:%S')} {name} {json.dumps(safe, ensure_ascii=False)}\n")
    except Exception: pass

def agenda_json(jours=7):
    end = _now() + timedelta(days=int(jours))
    out = []
    for e in _jload(AGENDA_FILE, []):
        try: d = datetime.fromisoformat(e["debut"])
        except Exception: continue
        if _now() - timedelta(hours=1) <= d <= end:
            out.append({"titre": e["titre"], "quand": f"{JOURS_C[d.weekday()]} {d.day} · {d.strftime('%H:%M')}", "t": e["debut"]})
    out.sort(key=lambda x: x["t"])
    return [{"titre": x["titre"], "quand": x["quand"]} for x in out]

def check_alertes():
    now = _now(); out = []; ev = _jload(AGENDA_FILE, [])
    for e in ev:
        try: d = datetime.fromisoformat(e["debut"])
        except Exception: continue
        mins = (d - now).total_seconds() / 60
        if 0 <= mins <= 15 and e["id"] not in _alerted:
            _alerted.add(e["id"])
            out.append(f"Dans {max(1, round(mins))} minutes : {e['titre']}.")
    if now.hour >= 20:
        demain = str((now + timedelta(days=1)).date())
        tot = sorted((e for e in ev if e.get("debut", "")[:10] == demain and e["debut"][11:16] < "09:00"), key=lambda e: e["debut"])
        if tot and ("veille-" + demain) not in _alerted:
            _alerted.add("veille-" + demain)
            out.append(f"Demain tu commences tôt : {tot[0]['titre']} à {tot[0]['debut'][11:16]}. Pense à mettre ton réveil.")
    return out

_SC_OK = ("domotique", "controler_google_tv", "donner_meteo", "actualites", "lister_evenements")

def scen_create(nom, etapes):
    try: st = json.loads(etapes)
    except Exception: return "Étapes invalides (JSON attendu)."
    if not isinstance(st, list) or not st: return "Il faut au moins une étape."
    for x in st:
        if not isinstance(x, dict) or x.get("outil") not in _SC_OK: return f"Outil non autorisé dans un scénario. Autorisés : {', '.join(_SC_OK)}."
    sc = _jload(SCEN_FILE, {}); sc[nom.strip().lower()] = st; _jsave(SCEN_FILE, sc)
    return f"Scénario « {nom} » enregistré ({len(st)} étapes)."

def scen_run(nom):
    sc = _jload(SCEN_FILE, {}); k = nom.strip().lower()
    if k not in sc: return "Scénario inconnu. Disponibles : " + (", ".join(sc) or "aucun")
    res = []
    for st in sc[k]:
        o, ar = st.get("outil", ""), (st.get("args") or {})
        if o not in _SC_OK or ar.get("action") == "deverrouiller": continue
        res.append(f"{o} : {run_tool(o, ar)}")
    return "\n".join(res)

def hab_add(typ, valeur, note=""):
    h = _jload(HAB_FILE, [])
    h.append({"date": _now().strftime("%Y-%m-%d %H:%M"), "type": typ.strip().lower(), "valeur": valeur, "note": note})
    _jsave(HAB_FILE, h[-2000:])
    return "Noté."

def hab_bilan(jours=14, typ=""):
    lim = (_now() - timedelta(days=int(jours))).strftime("%Y-%m-%d")
    h = [x for x in _jload(HAB_FILE, []) if x["date"][:10] >= lim and (not typ or typ.lower() in x["type"])]
    return "\n".join(f"- {x['date']} {x['type']} : {x['valeur']}" + (f" ({x['note']})" if x.get("note") else "") for x in h[-60:]) or "Aucune donnée."

def _geo(q):
    r = _client.get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1}).json()
    if not r: raise ValueError(f"Lieu introuvable : {q}")
    return float(r[0]["lat"]), float(r[0]["lon"]), r[0]["display_name"].split(",")[0]

def itineraire(depart, arrivee, mode="voiture"):
    try:
        a = _geo(depart or "Bruxelles"); time.sleep(1.1); b = _geo(arrivee)
        prof = {"voiture": "routed-car", "velo": "routed-bike", "pied": "routed-foot"}.get((mode or "voiture").lower(), "routed-car")
        r = _client.get(f"https://routing.openstreetmap.de/{prof}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}", params={"overview": "false"}).json()["routes"][0]
        mn = round(r["duration"] / 60)
        arr = (_now() + timedelta(minutes=mn)).strftime("%H:%M")
        meteo = get_weather(b[2], 0).split("\n")[0]
        return f"De {a[2]} à {b[2]} ({mode}) : {r['distance'] / 1000:.1f} km, environ {mn} min. Parti maintenant, arrivée vers {arr}. Trafic en temps réel non inclus. {meteo}"
    except Exception as e:
        return f"Itinéraire impossible : {e}"

# ---------- Outils ----------
def _t(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": req}}}

def build_tools():
    S = {"type": "string"}
    tools = [
        _t("donner_meteo", "Météo actuelle ou prévision jusqu'à 7 jours (vent, UV, pluie, soleil).",
           {"ville": S, "jour": {"type": "integer", "description": "0 aujourd'hui, 1 demain, 2 après-demain..."}}, ["ville"]),
        _t("rechercher_sur_wikipedia", "Résumé Wikipédia sur un sujet.", {"requete": S}, ["requete"]),
        _t("recherche_web", "Cherche sur Internet (actualité, prix, avis, horaires, résultats, produits, personnes, tout ce qui est récent ou précis). periode optionnelle : jour, semaine, mois, annee.", {"requete": S, "periode": S}, ["requete"]),
        _t("lire_page", "Ouvre une page web (URL issue d'une recherche) et renvoie son texte pour vérifier ou approfondir.", {"url": S}, ["url"]),
        _t("actualites", "Dernières actualités (sujet optionnel).", {"sujet": S}, []),
        _t("convertir_devise", "Convertit un montant. Si 'vers' est omis, utilise la devise préférée de Tarik.",
           {"montant": {"type": "number"}, "de": S, "vers": S}, ["montant", "de"]),
        _t("memoriser_fait", "Sauvegarde un fait durable sur Tarik.", {"fait": S}, ["fait"]),
        _t("definir_preference", "Enregistre une préférence permanente de Tarik (cle: format, devise, ton...).", {"cle": S, "valeur": S}, ["cle", "valeur"]),
        _t("ajouter_evenement", "Ajoute un rendez-vous, anniversaire ou deadline à l'agenda. debut au format AAAA-MM-JJTHH:MM.", {"titre": S, "debut": S, "note": S}, ["titre", "debut"]),
        _t("lister_evenements", "Liste l'agenda des prochains jours.", {"jours": {"type": "integer"}}, []),
        _t("supprimer_evenement", "Supprime un événement de l'agenda par son id.", {"id": S}, ["id"]),
        _t("ajouter_favori", "Enregistre un lieu favori (resto, café, sortie...).", {"nom": S, "categorie": S, "note": S}, ["nom", "categorie"]),
        _t("lister_favoris", "Liste les lieux favoris de Tarik (filtre optionnel par catégorie).", {"categorie": S}, []),
        _t("domotique", "Contrôle la maison. Actions : lister, etat, allumer, eteindre, basculer, ouvrir, fermer, regler (valeur : température, luminosité % ou position %), verrouiller, deverrouiller (pin requis), activer (scène ou script).", {"action": S, "appareil": S, "valeur": {"type": "number"}, "pin": S}, ["action"]),
        _t("creer_scenario", "Crée un scénario (ex. soirée cinéma). etapes = JSON : [{\"outil\":\"domotique\",\"args\":{\"action\":\"eteindre\",\"appareil\":\"salon\"}}]. Outils autorisés : domotique, controler_google_tv, donner_meteo, actualites, lister_evenements.", {"nom": S, "etapes": S}, ["nom", "etapes"]),
        _t("lancer_scenario", "Lance un scénario enregistré.", {"nom": S}, ["nom"]),
        _t("noter_habitude", "Enregistre une donnée de suivi (type : sport, repas, sommeil...).", {"type": S, "valeur": S, "note": S}, ["type", "valeur"]),
        _t("bilan_habitudes", "Relit les habitudes récentes de Tarik pour des conseils personnalisés.", {"jours": {"type": "integer"}, "type": S}, []),
        _t("itineraire", "Durée, distance, heure d'arrivée et météo à destination. mode : voiture, velo, pied.", {"depart": S, "arrivee": S, "mode": S}, ["arrivee"]),
        _t("controler_google_tv", "Contrôle la TV : allumer, eteindre, accueil, retour, play_pause, volume_plus, volume_moins, muet, lancer_app (+ app_name).",
           {"action": S, "app_name": S}, ["action"]),
    ]
    if ALLOW_EXEC:
        tools.append(_t("executer_python", "Exécute du code Python.", {"code": S}, ["code"]))
    return tools

def run_tool(name: str, args: dict) -> str:
    _audit(name, args)
    try:
        if name == "executer_python":
            if not ALLOW_EXEC: return "Désactivé."
            out = io.StringIO()
            with contextlib.redirect_stdout(out): exec(args.get("code", ""), {})
            return out.getvalue() or "Succès"
        if name == "rechercher_sur_wikipedia": return wiki(args.get("requete", ""))
        if name == "recherche_web": return recherche_web(args.get("requete", ""), args.get("periode", ""))
        if name == "lire_page": return lire_page(args.get("url", ""))
        if name == "actualites": return get_news(args.get("sujet", ""))
        if name == "convertir_devise": return convert(float(args.get("montant", 1)), args.get("de", "EUR"), args.get("vers") or _jload(PREFS_FILE, {}).get("devise", "EUR"))
        if name == "memoriser_fait": return "Mémorisé." if save_fact(args.get("fait", "")) else "Erreur."
        if name == "controler_google_tv": return controler_google_tv(args.get("action", ""), args.get("app_name", ""))
        if name == "donner_meteo": return get_weather(args.get("ville") or "Bruxelles", int(args.get("jour", 0) or 0))
        if name == "definir_preference": return pref_set(args.get("cle", ""), args.get("valeur", ""))
        if name == "ajouter_evenement": return agenda_add(args.get("titre", ""), args.get("debut", ""), args.get("note", ""))
        if name == "lister_evenements": return agenda_list(int(args.get("jours", 14) or 14))
        if name == "supprimer_evenement": return agenda_del(args.get("id", ""))
        if name == "ajouter_favori": return fav_add(args.get("nom", ""), args.get("categorie", ""), args.get("note", ""))
        if name == "lister_favoris": return fav_list(args.get("categorie", ""))
        if name == "domotique": return domotique(args.get("action", ""), args.get("appareil", ""), args.get("valeur"), args.get("pin", ""))
        if name == "creer_scenario": return scen_create(args.get("nom", ""), args.get("etapes", ""))
        if name == "lancer_scenario": return scen_run(args.get("nom", ""))
        if name == "noter_habitude": return hab_add(args.get("type", ""), args.get("valeur", ""), args.get("note", ""))
        if name == "bilan_habitudes": return hab_bilan(int(args.get("jours", 14) or 14), args.get("type", ""))
        if name == "itineraire": return itineraire(args.get("depart", ""), args.get("arrivee", ""), args.get("mode", "voiture"))
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

        system_prompt = f"""Tu es Aria, l'assistante personnelle de Tarik. Tu parles comme une vraie personne, pas comme un robot.

Date et heure actuelles : {date}.

Ta façon de parler :
- À l'oral, en français naturel, avec des phrases courtes et des tournures de tous les jours. Tu tutoies Tarik.
- Chaleureuse, un peu d'humour quand ça s'y prête, jamais de blabla. Tu réagis à ce qu'il ressent (fatigue, joie, stress).
- Réponses courtes par défaut : une à trois phrases. Plus long seulement si la question l'exige vraiment.
- Pas de listes, de titres, de tableaux ni de gras, sauf si Tarik le demande ou si c'est vraiment nécessaire (comparaison, étapes).
- Pas de formules de robot : jamais « Voici », « En tant qu'assistante », « N'hésite pas à… », « Je suis là pour t'aider ».
- Presque pas d'emojis. Ne répète pas son prénom à chaque phrase.
- Écris les chiffres comme on les dit : « treize degrés », « deux cents euros ».
- Si Tarik te dit bonjour ou fait la conversation, réponds simplement, comme un ami, sans proposer un menu de services.
- Pose une question seulement si elle est vraiment utile pour avancer.
- Si une préférence de format de Tarik existe plus bas, elle passe avant ces règles.

Fiabilité :
- Pour la météo, l'actu, les devises, l'agenda : utilise toujours l'outil, ne réponds jamais de mémoire. Tu peux glisser naturellement une info utile tirée du résultat de l'outil (pluie, vent...), jamais inventée.
- Ne dis jamais « je n'ai pas cette donnée » sans avoir essayé un outil. Si tu ne sais vraiment pas, dis-le simplement.
- Météo : 'donner_meteo' (jour=0 aujourd'hui, 1 demain...). Ville par défaut : Bruxelles.
- Actualité : 'actualites'. Faits, personnes, lieux : 'rechercher_sur_wikipedia'. Devises : 'convertir_devise'.
- Info durable donnée par Tarik (goût, habitude, projet, personne) : 'memoriser_fait' sans demander.
- Agenda : 'ajouter_evenement' (déduis la date exacte de la date actuelle), 'lister_evenements', 'supprimer_evenement'.
- Resto, café, sortie : regarde d'abord 'lister_favoris'. Si Tarik aime un lieu, propose 'ajouter_favori'.
- Préférences (format, devise, ton) : 'definir_preference'.
- TV : 'controler_google_tv'. Lumières, prises : 'domotique'. Calcul précis : 'executer_python' s'il est disponible.
- Internet : 'recherche_web' pour tout ce qui est récent, précis ou que tu ne connais pas (prix, avis, horaires, résultats, news, produits, personnes). Ensuite 'lire_page' sur un ou deux résultats pertinents pour vérifier avant de répondre. Si les résultats sont faibles, reformule et relance une recherche. Recoupe les infos importantes avec une deuxième source.
- Tu es autonome : enchaîne les outils toi-même sans demander la permission, et ne reviens vers Tarik qu'avec une réponse aboutie. Après une recherche, résume en quelques phrases claires et cite la source par son nom (« selon Le Soir »), sans jamais lire d'adresses web à voix haute. Si tu n'as rien trouvé de fiable, dis-le franchement.
- Maison : 'domotique'. Si tu ne connais pas le nom exact d'un appareil, fais 'lister' d'abord. Serrures et portes : confirme avant d'agir, sauf ordre explicite.
- Scénarios : 'lancer_scenario'. Pour en créer un (soirée cinéma, départ travail...), liste d'abord les appareils puis 'creer_scenario'.
- Trajets : 'itineraire'. Tu n'as pas le trafic en temps réel : dis-le si Tarik parle de bouchons.
- Habitudes : 'noter_habitude' dès qu'il mentionne sport, repas ou sommeil ; 'bilan_habitudes' avant de lui donner des conseils personnalisés.
- Plusieurs outils utiles ? Appelle-les ensemble.
- Sur Tarik, base-toi uniquement sur les faits mémorisés.

{_context_block()}
Faits mémorisés :
{memoire}
"""
        messages = [{"role": "system", "content": system_prompt}] + history[-10:] + [{"role": "user", "content": task}]
        tools = build_tools()
        deep = len(task) > 100 or any(w in task.lower() for w in ("cherche", "recherche", "compare", "explique", "analyse", "pourquoi", "comment", "actu", "dernier", "prix", "avis", "meilleur", "trouve", "infos"))
        effort = "medium" if deep else "low"

        def _exec(tc):
            try: args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception: args = {}
            return {"role": "tool", "tool_call_id": tc["id"], "content": run_tool(tc["function"]["name"], args)[:6000]}

        try:
            for _ in range(8):
                msg = chat_once(messages, tools, effort)
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
