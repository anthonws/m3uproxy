FROM python:3.12-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py .

# Probe /health (cheap; no synchronous playlist fetch). Honors PROXY_PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request\ntry:\n    r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PROXY_PORT','7654')+'/health', timeout=4)\n    sys.exit(0 if r.status==200 else 1)\nexcept Exception:\n    sys.exit(1)"]

CMD ["python", "-u", "proxy.py"]
