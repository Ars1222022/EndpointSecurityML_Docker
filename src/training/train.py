import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import joblib
import mlflow
import mlflow.sklearn
import pandas as pd
import psycopg
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split

SUSPICIOUS_PROCESSES = {"powershell.exe", "cmd.exe", "wannacry.exe", "mimikatz.exe"}


def run_training() -> dict[str, float | str]:
    data_dir = Path(os.getenv("DATA_DIR", "data/raw"))
    model_dir = Path(os.getenv("MODEL_DIR", "models/production"))
    runs_dir = Path(os.getenv("RUNS_DIR", "data/runs"))
    database_url = os.getenv("APP_DATABASE_URL")
    if database_url:
        with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT process_name, network_connections, is_attack "
                "FROM endpoint_activities ORDER BY activity_id"
            )
            columns = [description.name for description in cursor.description]
            frame = pd.DataFrame(cursor.fetchall(), columns=columns)
        frame = frame.rename(columns={
            "process_name": "ProcessName",
            "network_connections": "NetworkConnections",
            "is_attack": "IsAttack",
        })
        source = "postgres:endpoint_activities"
    else:
        files = glob.glob(str(data_dir / "*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV training data found in {data_dir}")
        frame = pd.concat((pd.read_csv(file) for file in sorted(files)), ignore_index=True)
        source = ",".join(Path(file).name for file in sorted(files))

    if frame.empty:
        raise ValueError("Ingen träningsdata finns. Ladda exempeldata i PostgreSQL först.")
    required = {"NetworkConnections", "ProcessName", "IsAttack"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Training CSV is missing columns: {', '.join(sorted(missing))}")
    if frame["IsAttack"].nunique() < 2:
        raise ValueError("Training data must contain both normal (0) and attack (1) rows")

    features = pd.DataFrame({
        "NetworkConnections": pd.to_numeric(frame["NetworkConnections"], errors="raise"),
        "IsSuspicious": frame["ProcessName"].astype(str).str.strip().str.lower().isin(SUSPICIOUS_PROCESSES).astype(int),
    })
    labels = pd.to_numeric(frame["IsAttack"], errors="raise").astype(int)
    x_train, x_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.25, random_state=42, stratify=labels
    )
    model = RandomForestClassifier(n_estimators=100, random_state=42, class_weight="balanced")
    model.fit(x_train, y_train)
    predicted = model.predict(x_test)
    metrics = {
        "accuracy": float(accuracy_score(y_test, predicted)),
        "precision": float(precision_score(y_test, predicted, zero_division=0)),
        "recall": float(recall_score(y_test, predicted, zero_division=0)),
        "f1_score": float(f1_score(y_test, predicted, zero_division=0)),
    }

    model_dir.mkdir(parents=True, exist_ok=True)
    version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    model_path = model_dir / f"endpoint_model_{version}.pkl"
    joblib.dump(model, model_path)
    metrics["data_source"] = source
    metrics["model_version"] = version
    metrics["model_path"] = str(model_path)

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment("EndpointSecurityML_Docker")
        with mlflow.start_run() as run:
            mlflow.log_params({"model_type": "RandomForestClassifier", "n_estimators": 100, "training_rows": len(frame), "data_source": source})
            mlflow.log_metrics({key: value for key, value in metrics.items() if isinstance(value, (int, float))})
            mlflow.sklearn.log_model(model, "model")
            mlflow.set_tag("model_version", version)
            metrics["mlflow_run_id"] = run.info.run_id

    runs_dir.mkdir(parents=True, exist_ok=True)
    run_record = {**metrics, "created_at": datetime.now(timezone.utc).isoformat()}
    (runs_dir / f"{version}.json").write_text(json.dumps(run_record, indent=2), encoding="utf-8")
    print(f"Training complete: {metrics}")
    return metrics


if __name__ == "__main__":
    run_training()