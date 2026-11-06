FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py materials.py automation.py automation_web.py ./
COPY static/ static/
COPY templates/ templates/
COPY LICENSE FORK.md ./

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)"
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "16", \
     "--timeout", "120", "app:app"]
