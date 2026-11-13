from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine, migrate
from app import models  # noqa: F401  (importing registers the tables)
from app.routers import auth as auth_router
from app.routers import scenarios as scenarios_router
from app.routers import rules_profiles as rp_router
from app.routers import ops as ops_router
from app.routers import agent as agent_router

app = FastAPI(title="Demo Overlay API")

Base.metadata.create_all(bind=engine)
migrate(engine)

app.include_router(auth_router.router)
app.include_router(scenarios_router.router)
app.include_router(rp_router.router)
app.include_router(ops_router.router)
app.include_router(agent_router.router)

app.mount("/admin", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="admin")
app.mount("/monitor", StaticFiles(directory=Path(__file__).parent / "static" / "monitor", html=True), name="monitor")


@app.get("/health")
def health():
    return {"status": "ok"}
