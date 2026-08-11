FROM python:3.12-slim

WORKDIR /app

# opencv-python-headless needs no system GUI libs

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py ./

CMD ["python", "bot.py"]
