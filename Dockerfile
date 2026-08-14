FROM python:3.11-slim

# Instalar Tesseract OCR
RUN apt-get update && apt-get install -y tesseract-ocr && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# El puerto lo asigna Render mediante la variable PORT
EXPOSE 8080

CMD ["python", "bot_bollinger.py"]