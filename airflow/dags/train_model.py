from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator

from src.training.train import run_training


with DAG(
    dag_id="endpointsecurity_train_model",
    description="Train and register an endpoint threat detection model",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False,
    tags=["endpointsecurity", "ml"],
) as dag:
    PythonOperator(task_id="train_and_log_model", python_callable=run_training)