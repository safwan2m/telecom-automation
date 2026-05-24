FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir influxdb-client==1.44.0

COPY core_simulator.py .

CMD ["python", "-u", "core_simulator.py"]
