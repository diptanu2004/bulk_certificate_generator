from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.db import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Bulk Certificate Generator",
    version="1.0.0",
    summary="Submit one request with many recipients; poll the job; download the PDFs.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}
