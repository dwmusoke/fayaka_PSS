FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data
ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
