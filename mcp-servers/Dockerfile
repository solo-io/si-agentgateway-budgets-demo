FROM python:3.12-slim
RUN pip install --no-cache-dir "mcp>=1.9,<2"
COPY server.py /app/server.py
USER 65532
CMD ["python", "/app/server.py"]
