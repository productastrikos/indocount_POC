"""Start the API + websocket server on the port given by the PORT environment variable (default 8000)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                log_level="warning")
