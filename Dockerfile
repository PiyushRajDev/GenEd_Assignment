FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY mastery_service ./mastery_service

EXPOSE 8000

CMD ["uvicorn", "mastery_service.main:app", "--host", "0.0.0.0", "--port", "8000"]