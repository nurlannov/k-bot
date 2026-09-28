FROM python:3.11-slim

WORKDIR /app

# зависимости отдельным слоем — кеш работает при пересборках
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код
COPY kassir_core.py bot_telegram.py ./

# config.py не копируем — секреты приходят через env vars от Fly

CMD ["python", "-u", "bot_telegram.py"]