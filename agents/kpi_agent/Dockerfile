FROM python:3.12-slim
WORKDIR /app

# CPU-only PyTorch first (separate step so Docker layer-caches it)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY model.py train.py kpi_agent.py ./

CMD ["python", "-u", "kpi_agent.py"]
