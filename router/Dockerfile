FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV ROUTER_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=2s --start-period=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/__router/ping', timeout=2).status == 200 else 1)" || exit 1

CMD ["python", "app.py"]
