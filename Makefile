.PHONY: dev test up down

dev:
	cd backend && python -m uvicorn app.main:app --reload --port 8010

test:
	cd backend && python -m pytest -q

up:
	docker compose up -d

down:
	docker compose down
