FROM cloudflare/cloudflared:2026.9.0 AS tunnel
FROM python:3.12-slim
COPY --from=tunnel /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY services/tunnel/run.py /srv/run.py
USER 10001
CMD ["python","/srv/run.py"]
