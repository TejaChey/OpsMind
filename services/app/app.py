"""Tiny web service that depends on Postgres. Its /health does a REAL database round trip, so
when Postgres is unreachable this app reports 503 even though the app process itself is fine.
That is the point of the dependency-down fault: the symptom is here, the cause is elsewhere."""
import os

import psycopg2
from flask import Flask, jsonify

app = Flask(__name__)


def db_connect():
    return psycopg2.connect(host=os.environ["DB_HOST"], port=5432, user=os.environ["DB_USER"],
                            password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_NAME"],
                            connect_timeout=2)


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
    except psycopg2.Error as e:
        return jsonify(status="unhealthy", db_ok=False,
                       error=str(e).strip().splitlines()[0]), 503
    return jsonify(status="healthy", db_ok=True), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
