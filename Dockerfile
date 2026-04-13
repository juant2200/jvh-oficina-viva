FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py OFFICE_SIM.html office_state.json index.html chat_agent.js ./

ENV STATE_DIR=/data
ENV HOST=0.0.0.0

CMD ["python3", "server.py"]
