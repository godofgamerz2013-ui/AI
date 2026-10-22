FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 appuser
COPY gateway ./gateway
USER appuser
EXPOSE 8080
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
