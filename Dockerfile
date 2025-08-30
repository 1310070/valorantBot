# Use official Python image
FROM python:3.11-slim

# Create working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY rec.py ./
COPY valorantBot2/ ./valorantBot2

# Expose the FastAPI port
EXPOSE 8190

# Run the Discord bot (which also starts the API server)
CMD ["python", "-m", "valorantBot2.bot"]
