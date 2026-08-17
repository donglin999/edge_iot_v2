FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and the shared Python 3.10 resolution baseline.
COPY requirements.txt constraints-py310.txt /app/

# Install Python dependencies
RUN python -m pip install --no-cache-dir \
    --constraint constraints-py310.txt \
    --requirement requirements.txt \
    && python -m pip check

# Copy project
COPY . /app/

# Expose port
EXPOSE 8000

# Default command (can be overridden in docker-compose)
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
