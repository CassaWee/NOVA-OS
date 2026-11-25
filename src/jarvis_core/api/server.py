from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
import hmac, json, os
from src.jarvis_core.adapters.groq_adapter import GroqAdapter, weather_header, check_alertes, agenda_json

app = FastAPI(title="NOVA OS - API")

@app.middleware("http")
async def garde(request, call_next):
    tok = os.getenv("NOVA_TOKEN", "")
    if request.url.path != "/":
        sent = request.headers.get("x-token", "")
        if not tok or not hmac.compare_digest(sent.encode(), tok.encode()):
            return JSONResponse({"error": "non autorisé"}, status_code=401)
    return await call_next(request)
MEMORY_FILE = "data/memory.json"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return []

def save_memory(history):
    os.makedirs("data", exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f: json.dump(history, f, ensure_ascii=False, indent=2)

CHAT_HISTORY = load_memory()

class ChatRequest(BaseModel): query: str

@app.get("/", response_class=HTMLResponse)
def read_index():
    with open("static/index.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/agenda")
def agenda():
    return {"events": agenda_json(7)}

@app.get("/alertes")
def alertes():
    return {"alertes": check_alertes()}

@app.get("/meteo")
def meteo():
    return weather_header("Bruxelles") or {}

class TTSRequest(BaseModel): text: str

@app.post("/tts")
async def tts(req: TTSRequest):
    import edge_tts
    text = req.text.strip()[:1000]
    if not text:
        return Response(status_code=400)
    voice = os.getenv("TTS_VOICE", "fr-FR-VivienneMultilingualNeural")
    audio = bytearray()
    try:
        async for c in edge_tts.Communicate(text, voice, rate="+4%").stream():
            if c["type"] == "audio": audio += c["data"]
    except Exception as e:
        print("❌ Erreur voix :", e)
        return Response(status_code=502)
    return Response(bytes(audio), media_type="audio/mpeg")

@app.post("/chat")
def chat(req: ChatRequest):
    global CHAT_HISTORY
    try:
        result = GroqAdapter.call(None, req.query, history=CHAT_HISTORY)
        CHAT_HISTORY.append({"role": "user", "content": req.query})
        CHAT_HISTORY.append({"role": "assistant", "content": result.get("message", "")})
        if len(CHAT_HISTORY) > 20: CHAT_HISTORY = CHAT_HISTORY[-20:]
        save_memory(CHAT_HISTORY)
        return {"response": result}
    except Exception as e:
        print("❌ Erreur serveur :", e)
        return {"response": {"status": "error", "message": "NOVA OS a rencontré un bug."}}
