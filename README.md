# VERA Challenge Bot

A FastAPI implementation of the VERA merchant assistant API contract for the challenge.

## Local run

```bash
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Endpoints

- GET /v1/healthz
- GET /v1/metadata
- POST /v1/context
- POST /v1/tick
- POST /v1/reply

## Validation

```bash
python -m pytest -q
```

## Docker

```bash
docker build -t vera-bot .
docker run -p 8000:8000 -e PORT=8000 vera-bot
```

## Deployment

This service is ready for platforms such as Render or Railway. Set the runtime command to:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```
