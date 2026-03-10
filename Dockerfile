FROM python:3.11-slim

WORKDIR /app

# Install build dependencies and the runtime requirements
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . /app

ENV A2A_HOST=0.0.0.0 A2A_PORT=9000
EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
