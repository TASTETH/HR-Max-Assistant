FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY korsik ./korsik
COPY frontend_dist ./frontend_dist
COPY tests ./tests
RUN mkdir -p /app/data
EXPOSE 8000
CMD ["uvicorn", "korsik.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
