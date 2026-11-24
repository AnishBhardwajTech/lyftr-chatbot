.PHONY: up down logs test build restart clean

# Start the whole stack: backend on :8000, frontend on :3000
up:
	docker compose up -d --build
	@echo "Backend:  http://localhost:8000/health"
	@echo "Frontend: http://localhost:3000"

down:
	docker compose down

logs:
	docker compose logs -f

restart: down up

# Backend tests (offline, no API keys needed)
test:
	cd backend && python -m pytest -q

# Stop and delete the SQLite volume too
clean:
	docker compose down -v
