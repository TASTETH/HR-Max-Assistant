FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
COPY onboarding/requirements.txt ./onboarding/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY onboarding ./onboarding
COPY database ./database
COPY certs ./certs
RUN useradd --create-home korsik
USER korsik
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
