from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import jobs as jobs_api
from .api import upload as upload_api
from .config import get_settings
from .db import Base, engine
from .logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Zoom→GHL Notes",
    description="Receives Zoom recordings, transcribes, summarizes, and creates notes in GHL.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(upload_api.router)
app.include_router(jobs_api.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "environment": settings.environment}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {"service": "zoom-ghl-notes", "version": "0.1.0"}
