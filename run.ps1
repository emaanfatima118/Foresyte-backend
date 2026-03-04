# Run FastAPI from ForeSyte_Backend with correct Python path
$env:PYTHONPATH = "src"
python -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
