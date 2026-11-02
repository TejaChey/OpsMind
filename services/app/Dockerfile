FROM python:3.11-slim

RUN pip install --no-cache-dir flask psycopg2-binary redis
COPY app.py /app.py
CMD ["python", "/app.py"]
