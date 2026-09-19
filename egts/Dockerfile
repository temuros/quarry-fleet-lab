# Приёмный шлюз бортов: EGTS и Wialon IPS в одну шину.
#   docker build -f egts/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY egts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY egts/gateway.py egts/protocol.py egts/skhema.py ./
COPY wialon/ips.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 7777 7778 8090
CMD ["python", "gateway.py"]
