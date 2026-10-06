"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .errors import ApiError, api_error_handler
from .routers import certificates, measurements


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(
    title="Lab Equipment Calibration Traceability",
    version="1.0.0",
    description="实验室设备校准证书登记与测量记录追溯服务",
    lifespan=lifespan,
)

app.add_exception_handler(ApiError, api_error_handler)

app.include_router(certificates.router, prefix="/api/v1/certificates", tags=["certificates"])
app.include_router(measurements.router, prefix="/api/v1/measurements", tags=["measurements"])


@app.get("/health", tags=["meta"])
async def health():
    pool = db.get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
