# Editube Backend

FastAPI backend for the Editube project.

## Prerequisites

- Python 3.10+ (tested here with Python 3.14)
- pip

## Run Locally

```bash
cd /Users/wonder/Documents/experimental/editube
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install email-validator
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

API root should be available at `http://127.0.0.1:8000/`.

## Environment Variables

The project uses values in `.env`, including:

- `DATABASE_URL`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`

## Notes

- `email-validator` is required by the current Pydantic model definitions and should be installed in the virtual environment.
- Some runtime warnings may appear for `orm_mode` because parts of the codebase still use Pydantic v1-style config keys.
