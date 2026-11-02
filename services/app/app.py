"""Tiny web service that depends on Postgres and Redis. Its /health does a REAL database round trip, so
when Postgres is unreachable this app reports 503 even though the app process itself is fine.
That is the point of the dependency-down fault: the symptom is here, the cause is elsewhere."""
import json
import os

import psycopg2
import redis
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


def check_db():
    """(ok, error). A real connect + SELECT 1."""
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return True, None
    except (psycopg2.Error, ValueError, KeyError, TypeError) as e:   # DB or bad config
        return False, str(e).strip().splitlines()[0]


def check_redis():
    """(ok, error). A real PING to the cache."""
    try:
        redis.Redis(host=os.environ["REDIS_HOST"], socket_connect_timeout=2,
                    socket_timeout=2).ping()
        return True, None
    except (redis.RedisError, OSError, KeyError) as e:
        return False, f"redis: {str(e).strip().splitlines()[0]}"


@app.get("/health")
def health():
    """Healthy only if BOTH dependencies answer. Each is reported separately so a reader can
    tell which one is the cause (the app itself is fine in either case)."""
    db_ok, db_err = check_db()
    redis_ok, redis_err = check_redis()
    if db_ok and redis_ok:
        return jsonify(status="healthy", db_ok=True, redis_ok=True), 200
    return jsonify(status="unhealthy", db_ok=db_ok, redis_ok=redis_ok,
                   error=db_err or redis_err), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
