FROM python:3.12-slim

# Meshtastic/pyserial need no build tools at runtime; keep the image lean.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MESH_WX_DB=/data/mesh-wx.db \
    MESH_WX_PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/data"]
EXPOSE 8000

# Liveness: mark the container unhealthy if the app stops responding. The app's
# own watchdog force-exits a wedged loop (so restart:unless-stopped relaunches
# it); this HEALTHCHECK makes that state visible to `docker ps` and orchestrators.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"]

CMD ["python", "-m", "app.main"]
