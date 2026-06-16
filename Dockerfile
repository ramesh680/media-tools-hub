# One image used by both services (the hub app and the upcoming-movies app).
FROM python:3.12-slim

WORKDIR /srv

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project.
COPY . .

# Free-tier default: the data-heavy IMDb tools are disabled.
ENV IMDB_ENABLED=false \
    PYTHONUNBUFFERED=1

# The host platform provides $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Default command runs the hub app. The upcoming-movies service overrides this
# (see render.yaml: dockerCommand).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
