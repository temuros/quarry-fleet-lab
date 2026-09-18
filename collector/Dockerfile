# Контекст сборки это корень репозитория, как и у остальных образов стенда.
#   docker build -f collector/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector/collector.py collector/journal.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "collector.py"]
