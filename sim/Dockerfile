# Контекст сборки это корень репозитория: борту нужны те же кодеки, что и
# приёмному шлюзу, а две копии одного протокола расходятся молча.
#   docker build -f sim/Dockerfile .
FROM python:3.11-slim
WORKDIR /app
COPY sim/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sim/simulator.py sim/egts_uplink.py sim/wialon_uplink.py sim/statement_uplink.py ./
COPY egts/protocol.py egts/skhema.py ./
COPY wialon/ips.py ./
COPY statement/zayavlenie.py statement/podpis.py statement/kluchi.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "simulator.py"]
