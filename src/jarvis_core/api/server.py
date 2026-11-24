from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import time, json, os
from src.jarvis_core.adapters.groq_adapter import GroqAdapter
app = FastAPI(title="NOVA OS - API")
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
        return {"response": {"status": "error", "message": "NOVA OS a rencontré un bug."}}
