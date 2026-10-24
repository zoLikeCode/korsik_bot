FROM python:3.12-slim
ENV PYTHONPATH=/srv PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd -r -u 10001 app
COPY backend/korsik ./korsik
COPY services/model_server/app.py ./runtime/model-server-app.py
COPY services/model_server/feedback/model.json /models/feedback/model.json
COPY seed ./seed
USER 10001
EXPOSE 8000
CMD ["uvicorn","korsik.main:app","--host","0.0.0.0","--port","8000","--workers","2","--no-access-log"]
