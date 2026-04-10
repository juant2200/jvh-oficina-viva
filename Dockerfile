FROM python:3.11-slim
WORKDIR /app
COPY server.py OFFICE_SIM.html office_state.json ./
ENV STATE_DIR=/data
ENV HOST=0.0.0.0
ENV PORT=8765
EXPOSE 8765
CMD ["python3", "server.py"]
