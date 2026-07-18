FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INFOFIN_WEB_HOST=0.0.0.0 \
    INFOFIN_WEB_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 infofin \
    && chown -R infofin:infofin /app
USER infofin

CMD ["python", "-m", "webapp.server"]
