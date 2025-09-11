FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# garante a pasta base (dentro do container) e permite escrita
RUN mkdir -p /app/media && chmod -R 777 /app/media

# expõe 80 (seu app já está subindo em 80)
EXPOSE 80
ENV PORT=80

CMD ["python", "app.py"]
