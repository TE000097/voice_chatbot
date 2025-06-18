# Use a process manager to run both servers
#RUN pip install 'uvicorn[standard]' gunicorn

FROM python:3.10-slim

RUN apt-get update && apt-get install -y gcc ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

RUN pip install 'uvicorn[standard]' gunicorn

COPY . .

EXPOSE 9000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]
