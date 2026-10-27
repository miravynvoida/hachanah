"""BotHost-friendly entry point. The FastAPI lifespan starts and stops Telegram polling."""
import os
import uvicorn
from app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3000")), log_level=os.getenv("LOG_LEVEL", "info").lower())
