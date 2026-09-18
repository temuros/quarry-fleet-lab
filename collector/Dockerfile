# Контекст сборки это корень репозитория, как и у остальных образов стенда.
#   docker build -f collector/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ⚠️ Файлы перечисляются поимённо, поэтому новый модуль надо добавлять и
# сюда. Забыл строку и образ собирается молча, а падает уже в кластере на
# «ModuleNotFoundError».
COPY collector/collector.py collector/journal.py collector/alerts.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8001
CMD ["python", "collector.py"]
