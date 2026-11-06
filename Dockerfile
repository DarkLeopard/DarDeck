FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости берём из pyproject.toml; pytest — dev-инструмент, ставим рядом.
COPY pyproject.toml ./
COPY engine/ ./engine/
COPY tests/ ./tests/

RUN pip install --no-cache-dir . pytest

# Non-root пользователь для запуска приложения и тестов
RUN useradd --create-home appuser
USER appuser

CMD ["pytest", "-q"]
