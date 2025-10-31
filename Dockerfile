FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Python deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . /app

# Create data dir for SQLite
RUN mkdir -p /data

# Expose port (gunicorn will bind 0.0.0.0:8000)
EXPOSE 5000

# Launch with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "120", "--graceful-timeout", "30", "--keep-alive", "2", "--log-level", "warning", "--preload","app:create_app()"]
