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

CMD ["python", "-m", "app.main"]
