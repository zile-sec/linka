FROM python:3.12-slim

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies first (cache layer)
COPY services/user-service/app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Copy shared packages
COPY packages /app/packages

# Copy the service code into its own namespace
COPY services/user-service/app /app/app

# Packages available via PYTHONPATH for `from shared.x import y`
ENV PYTHONPATH=/app/packages:/app:$PYTHONPATH

EXPOSE 8000

# Default command (overridden by docker-compose)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
