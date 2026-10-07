"""KubeDoor AI HTTP service plus backwards compatible MCP transports."""
import os
import sys
from pathlib import Path

shared = Path(__file__).resolve().parent.parent / "kubedoor-tools"
if shared.exists():
    sys.path.insert(0, str(shared))

from kubedoor_ai.http import create_app

app = create_app()

if __name__ == "__main__":
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info", access_log=False)
