# Шлюз EGTS: принимает телеметрию бортов и кладёт её в шину.
#   docker build -f egts/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY egts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY egts/gateway.py egts/protocol.py egts/skhema.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 7777 8090
CMD ["python", "gateway.py"]
