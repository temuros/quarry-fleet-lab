.PHONY: up down logs reset outage-on outage-off status metrics

up:
	docker compose up -d --build

down:
	docker compose down

reset:
	docker compose down -v

logs:
	docker compose logs -f sim-balanced collector

status:
	curl -s localhost:8081; echo; curl -s localhost:8082; echo

outage-on:
	curl -s -X POST localhost:8082/outage/start; echo

outage-off:
	curl -s -X POST localhost:8082/outage/stop; echo

metrics:
	curl -s localhost:8000/metrics | grep -E "^quarry_(tons_per_hour|cycle_seconds_avg|wait_seconds_avg|stream_uptime_ratio|ingest_lag_seconds)"
