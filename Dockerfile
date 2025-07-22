FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data && useradd -m botuser && \
    chown -R botuser:botuser /app

COPY --chown=botuser:botuser . .

USER botuser
CMD ["python", "bot.py"]
