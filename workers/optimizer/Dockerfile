# workers/optimizer/Dockerfile
# Furniture Layout Optimizer for AWS ECS (Fargate)

FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Install necessary build tools for scipy/numpy if needed
# (though many wheels are available, slim might need these for some versions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
# Note: This Dockerfile expects to be built from the project root
COPY workers/optimizer/requirements.txt /app/workers/optimizer/requirements.txt
RUN pip install --no-cache-dir -r /app/workers/optimizer/requirements.txt

# Copy the shared transformation logic and the optimizer worker
# The optimizer depends on server.services.transform
COPY server/ /app/server/
COPY workers/optimizer/ /app/workers/optimizer/

# Set working directory to the project root so that scripts
# can resolve relative paths (e.g. workers/optimizer/examples/...)
WORKDIR /app

# Default entry point
CMD ["python", "/app/workers/optimizer/run_s3_optimizer.py"]
