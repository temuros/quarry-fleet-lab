# Приёмный шлюз бортов: EGTS, Wialon IPS и подписанные заявления в одну шину.
#   docker build -f egts/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY egts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY egts/gateway.py egts/protocol.py egts/skhema.py ./
COPY wialon/ips.py ./
# Шлюз разбирает заявления, но подписи не проверяет: ключи ему не нужны.
COPY statement/zayavlenie.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 7777 7778 8090
EXPOSE 7779/udp
CMD ["python", "gateway.py"]
