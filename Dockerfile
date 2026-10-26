FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sensor ./sensor
COPY plc ./plc
COPY hmi ./hmi
ENV PYTHONUNBUFFERED=1
