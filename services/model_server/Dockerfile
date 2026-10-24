FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
WORKDIR /srv
COPY services/model_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 model
COPY services/model_server/app.py .
USER 10001
EXPOSE 8000
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000","--workers","1"]
