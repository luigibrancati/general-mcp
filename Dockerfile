FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY general_mcp ./general_mcp

RUN pip install --no-cache-dir . && \
    playwright install --with-deps chromium

ENV PLAYWRIGHT_BROWSERS_INSTALLED=1

CMD ["python", "-m", "general_mcp.server"]