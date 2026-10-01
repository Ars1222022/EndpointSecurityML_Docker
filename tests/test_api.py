import numpy as np
from fastapi.testclient import TestClient

from src.api.app import app


def test_health_reports_model_state():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "model_loaded" in response.json()


def test_prediction_returns_model_result(monkeypatch, tmp_path):
    model_path = tmp_path / "endpoint_model_test.pkl"
    model_path.touch()

    class FakeModel:
        def predict(self, features):
            return [1]

        def predict_proba(self, features):
            return np.asarray([[0.15, 0.85]])

    monkeypatch.setattr("src.api.app.latest_model_file", lambda: model_path)
    monkeypatch.setattr("src.api.app.joblib.load", lambda _: FakeModel())
    monkeypatch.setattr("src.api.app.DATABASE_URL", "")

    with TestClient(app) as client:
        response = client.post("/predict", json={"NetworkConnections": 1, "ProcessName": "notepad.exe"})

    assert response.status_code == 200
    assert response.json()["prediction"] == 1
    assert response.json()["confidence"] == 0.85
    assert response.json()["threat_type"] == "Attack"


def test_prediction_requires_a_trained_model(monkeypatch):
    monkeypatch.setattr("src.api.app.latest_model_file", lambda: None)

    with TestClient(app) as client:
        response = client.post("/predict", json={"NetworkConnections": 1, "ProcessName": "notepad.exe"})

    assert response.status_code == 503


def test_invalid_prediction_input_is_rejected():
    with TestClient(app) as client:
        response = client.post("/predict", json={"NetworkConnections": -1, "ProcessName": "notepad.exe"})

    assert response.status_code == 422