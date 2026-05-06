"""Entry point for the Unibet Tennis Odds dashboard."""

import logging

import uvicorn

from config import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    uvicorn.run(
        "server.app:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_level="info",
    )
