import os, io, json, time, hashlib, contextlib, urllib.parse, httpx
from dotenv import load_dotenv

load_dotenv()
PROFIL_FILE = "data/profil.json"
CHROMA_DIR = "data/chroma"
TV_IP = os.getenv("TV_IP", "192.168.1.50")
TV_PORT = 5555
ADB_KEY = "data/adbkey"
ALLOW_EXEC = os.getenv("JARVIS_ALLOW_EXEC") == "1"
MODEL = "openai/gpt-oss-20b"
URL = "https://api.groq.com/openai/v1/chat/completions"

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
            except: pass
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

def search_facts(question: str, k: int = 5) -> list:
    col = _get_collection()
    if not col: return []
    n = col.count()
    if n == 0: return []
    res = col.query(query_texts=[question], n_results=min(k, n))
    return res["documents"][0] if res.get("documents") else []

def controler_google_tv(action: str, app_name: str = "") -> str:
    try:
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner
    except ImportError: return "Erreur : installe adb-shell."

    action = (action or "").lower().strip()
    if action == "lancer_app":
        pkg = {"netflix": "com.netflix.ninja", "youtube": "com.google.android.youtube.tv", "disney": "com.disney.disneyplus", "prime video": "com.amazon.amazonvideo.livingroom", "spotify": "com.spotify.tv.android"}.get((app_name or "").lower().strip())
        if not pkg: return f"Application inconnue."
        cmd = f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
    else:
        keys = {"allumer": "KEYCODE_WAKEUP", "eteindre": "KEYCODE_SLEEP", "accueil": "KEYCODE_HOME", "retour": "KEYCODE_BACK", "play_pause": "KEYCODE_MEDIA_PLAY_PAUSE", "volume_plus": "KEYCODE_VOLUME_UP", "volume_moins": "KEYCODE_VOLUME_DOWN", "muet": "KEYCODE_VOLUME_MUTE"}
        if action in keys: cmd = f"input keyevent {keys[action]}"
        else: return f"Action inconnue."

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

def get_weather(city: str) -> str:
    try:
        r = httpx.get(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1", timeout=10.0)
        data = r.json()
        current = data["current_condition"][0]
        temp = current["temp_C"]
        desc = current["lang_fr"][0]["value"] if "lang_fr" in current else current["weatherDesc"][0]["value"]
        return f"À {city}, il fait actuellement {temp}°C avec {desc}."
    except: return f"Impossible de récupérer la météo pour {city}."

def build_tools():
    tools = [
        {"type": "function", "function": {"name": "rechercher_sur_wikipedia", "description": "Recherche sur Wikipédia.", "parameters": {"type": "object", "properties": {"requete": {"type": "string"}}, "required": ["requete"]}}},
        {"type": "function", "function": {"name": "memoriser_fait", "description": "Sauvegarde un fait durable.", "parameters": {"type": "object", "properties": {"fait": {"type": "string"}}, "required": ["fait"]}}},
        {"type": "function", "function": {"name": "controler_google_tv", "description": "Contrôle la TV.", "parameters": {"type": "object", "properties": {"action": {"type": "string"}, "app_name": {"type": "string"}}, "required": ["action"]}}},
        {"type": "function", "function": {"name": "donner_meteo", "description": "Donne la météo.", "parameters": {"type": "object", "properties": {"ville": {"type": "string"}}, "required": ["ville"]}}}
    ]
    if ALLOW_EXEC: tools.append({"type": "function", "function": {"name": "executer_python", "description": "Exécute du code Python.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}})
    return tools

def run_tool(name: str, args: dict) -> str:
    try:
        if name == "executer_python":
            if not ALLOW_EXEC: return "Désactivé."
            out = io.StringIO()
            with contextlib.redirect_stdout(out): exec(args.get("code", ""), {})
            return out.getvalue() or "Succès"
        if name == "rechercher_sur_wikipedia":
            w_url = f"https://fr.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(args.get('requete', ''))}&format=json"
            res = httpx.get(w_url, timeout=10.0).json().get("query", {}).get("search", [])
            return "\n".join(f"- {r['title']}" for r in res[:3]) or "Aucun résultat."
        if name == "memoriser_fait": return "Mémorisé." if save_fact(args.get("fait", "")) else "Erreur."
        if name == "controler_google_tv": return controler_google_tv(args.get("action", ""), args.get("app_name", ""))
        if name == "donner_meteo": return get_weather(args.get("ville", "Bruxelles"))
        return f"Outil inconnu : {name}"
    except Exception as e: return f"Erreur outil {name} : {e}"

def post_with_retry(url, headers, json_data, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = httpx.post(url, headers=headers, json=json_data, timeout=30.0)
            if r.status_code == 429:
                time.sleep(int(float(r.headers.get("retry-after", 5))))
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPError:
            if attempt == max_retries - 1: raise
            time.sleep(2)
    return None

class GroqAdapter:
    @staticmethod
    def call(provider: dict, task: str, history: list = None):
        api_key = os.getenv("GROQ_API_KEY")
        history = history or []
        faits = search_facts(task, k=5)
        memoire = "\n".join(f"- {f}" for f in faits) if faits else "Aucun fait pertinent."

        system_prompt = f"""Tu es A.R.I.A. (Assistant de Réponse et d'Intelligence Autonome), l'assistante personnelle de Tarik.
Tu fais partie du système NOVA OS. Ta personnalité : futuriste, élégante, efficace, avec une pointe d'humour.
RÈGLE ABSOLUE : tu TUTOIES Tarik en toute circonstance. Sois directe et concise. Utilise le Markdown.
Si on te demande la météo, utilise 'donner_meteo'. Si Tarik donne une info durable, utilise 'memoriser_fait'.

Faits mémorisés :
{memoire}
"""
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": task}]
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            r = post_with_retry(URL, headers, {"model": MODEL, "messages": messages, "tools": build_tools(), "tool_choice": "auto", "temperature": 0.7})
            if not r: return {"status": "error", "message": "Trop de requêtes."}
            
            msg = r.json()["choices"][0]["message"]
            if msg.get("tool_calls"):
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": msg["tool_calls"]})
                for tc in msg["tool_calls"]:
                    try: args = json.loads(tc["function"]["arguments"] or "{}")
                    except: args = {}
                    result = run_tool(tc["function"]["name"], args)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

                r2 = post_with_retry(URL, headers, {"model": MODEL, "messages": messages})
                if not r2: return {"status": "error", "message": "Trop de requêtes."}
                return {"status": "success", "message": r2.json()["choices"][0]["message"]["content"] or ""}

            return {"status": "success", "message": msg.get("content") or ""}
        except Exception as e:
            return {"status": "error", "message": "Petite panne technique, reformule."}
