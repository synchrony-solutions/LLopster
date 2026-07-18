FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py helm-values.yaml ./
COPY check_helm_values.py check_db_pool.py check_cache.py check_upstream.py check_scheduler.py ./

CMD ["python", "-u", "main.py"]
