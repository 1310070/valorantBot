# Use official Python image
FROM python:3.11-slim

# Create working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY valorantBot2/ .

# Run the bot
CMD ["python", "bot.py"]
