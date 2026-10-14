"""Tiny web service that depends on Postgres. Its /health does a REAL database round trip, so
when Postgres is unreachable this app reports 503 even though the app process itself is fine.
That is the point of the dependency-down fault: the symptom is here, the cause is elsewhere."""
import json
import os

import psycopg2
from flask import Flask, jsonify

app = Flask(__name__)


CONFIG_FILE = "/config/app.json"   # the "deployed config"; replaced by each deploy


def load_config():
    """Re-read on every request so a deploy takes effect immediately. Env vars are the
    defaults; the config file overrides them. A missing file means "use the defaults"; an
    unreadable one is reported as unhealthy rather than guessed at."""
    cfg = {"db_host": os.environ["DB_HOST"], "db_port": 5432, "connect_timeout_seconds": 2}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def db_connect():
    cfg = load_config()
    return psycopg2.connect(host=cfg["db_host"], port=cfg["db_port"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_NAME"],
                            connect_timeout=cfg["connect_timeout_seconds"])


@app.get("/health")
def health():
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except (psycopg2.Error, ValueError, KeyError, TypeError) as e:   # DB or bad config
        return jsonify(status="unhealthy", db_ok=False,
                       error=str(e).strip().splitlines()[0]), 503
    return jsonify(status="healthy", db_ok=True), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
