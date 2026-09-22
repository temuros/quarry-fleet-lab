# Контекст сборки это корень репозитория, как и у остальных образов стенда.
#   docker build -f collector/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# ⚠️ Файлы перечисляются поимённо, поэтому новый модуль надо добавлять и
# сюда. Забыл строку и образ собирается молча, а падает уже в кластере на
# «ModuleNotFoundError».
COPY collector/collector.py collector/journal.py collector/alerts.py collector/podpisi.py ./
# Кодек заявлений, ключи и общий справочник полей. Приёмник проверяет подпись
# тем же кодом, которым борт её ставит: две копии кодека разошлись бы молча.
COPY statement/zayavlenie.py statement/podpis.py statement/kluchi.py ./
COPY statement/kluchi-bortov.json ./
COPY egts/skhema.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8001
CMD ["python", "collector.py"]
