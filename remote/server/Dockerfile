FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV SPR_DATA_DIR=/data
EXPOSE 8642
# --proxy-headers: เชื่อ X-Forwarded-Proto จาก reverse proxy (ให้ secure cookie ทำงาน)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8642", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
