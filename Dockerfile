FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY steam_sniper.py .

CMD ["python", "steam_sniper.py"]
