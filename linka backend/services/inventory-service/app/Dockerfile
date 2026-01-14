FROM python:3.12-slim

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY services/inventory-service/app/requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the application code
COPY services/inventory-service/app .

# Copy shared packages for imports
COPY packages /app/packages

# Add packages to Python path
ENV PYTHONPATH=/app/packages:$PYTHONPATH

# Expose the port the app runs on
EXPOSE 8000
