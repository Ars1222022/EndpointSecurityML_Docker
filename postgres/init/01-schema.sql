-- PostgreSQL 16 only; this is not T-SQL and must not be run in SQL Server.
-- Docker runs this initialization script only for a new Postgres data volume.
CREATE DATABASE airflow OWNER admin;

CREATE TABLE endpoint_activities (
    activity_id BIGSERIAL PRIMARY KEY,
    process_name TEXT NOT NULL,
    network_connections INTEGER NOT NULL CHECK (network_connections >= 0),
    is_attack SMALLINT NOT NULL CHECK (is_attack IN (0, 1)),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COPY endpoint_activities (process_name, network_connections, is_attack)
FROM '/seed/endpoint_activities.csv'
WITH (FORMAT csv, HEADER true);

CREATE TABLE prediction_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    network_connections INTEGER NOT NULL CHECK (network_connections >= 0),
    process_name TEXT NOT NULL,
    prediction SMALLINT NOT NULL CHECK (prediction IN (0, 1)),
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    model_version TEXT NOT NULL
);