import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import numpy as np
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from src.api.schemas import PredictionRequest, PredictionResponse

MODEL_DIR = Path(os.getenv("MODEL_DIR", "models/production"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
SUSPICIOUS_PROCESSES = {"powershell.exe", "cmd.exe", "wannacry.exe", "mimikatz.exe"}
REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "path"])
PREDICTIONS = Counter("predictions_total", "Model predictions", ["threat_type"])


def latest_model_file() -> Path | None:
    model_files = list(MODEL_DIR.glob("endpoint_model_*.pkl"))
    return max(model_files, key=lambda path: path.stat().st_mtime) if model_files else None


def load_model_if_changed(app: FastAPI) -> None:
    model_path = latest_model_file()
    if model_path is None:
        app.state.model = None
        app.state.model_path = None
        return
    if model_path != getattr(app.state, "model_path", None):
        app.state.model = joblib.load(model_path)
        app.state.model_path = model_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = None
    app.state.model_path = None
    load_model_if_changed(app)
    yield


app = FastAPI(title="Endpoint Security ML API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    REQUESTS.labels(request.method, request.url.path, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, request.url.path).observe(elapsed)
    return response


@app.get("/")
def root():
    return {"name": "Endpoint Security ML API", "docs": "/docs", "health": "/health", "metrics": "/metrics"}


@app.get("/health")
def health(request: Request):
    load_model_if_changed(request.app)
    path = request.app.state.model_path
    return {"status": "ok", "model_loaded": path is not None, "model_version": path.stem.removeprefix("endpoint_model_") if path else None}


@app.post("/predict", response_model=PredictionResponse)
def predict(payload: PredictionRequest, request: Request):
    load_model_if_changed(request.app)
    model = request.app.state.model
    model_path = request.app.state.model_path
    if model is None or model_path is None:
        raise HTTPException(status_code=503, detail="No model is trained yet. Run the training step first.")

    suspicious = int(payload.ProcessName.strip().lower() in SUSPICIOUS_PROCESSES)
    features = np.asarray([[payload.NetworkConnections, suspicious]], dtype=np.float64)
    prediction = int(model.predict(features)[0])
    confidence = float(model.predict_proba(features).max())
    threat_type = "Attack" if prediction else "Normal"
    version = model_path.stem.removeprefix("endpoint_model_")

    if DATABASE_URL:
        try:
            with psycopg.connect(DATABASE_URL) as connection:
                connection.execute(
                    "INSERT INTO prediction_events (network_connections, process_name, prediction, confidence, model_version) VALUES (%s, %s, %s, %s, %s)",
                    (payload.NetworkConnections, payload.ProcessName, prediction, confidence, version),
                )
        except psycopg.Error as error:
            raise HTTPException(status_code=503, detail="Prediction storage is unavailable") from error

    PREDICTIONS.labels(threat_type=threat_type).inc()
    return PredictionResponse(prediction=prediction, confidence=confidence, threat_type=threat_type, model_version=version)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)