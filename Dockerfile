FROM python:3.12-slim
WORKDIR /app
COPY ac_rating.py server.py admin.py admin.html stats.html backup.sh /app/
RUN pip install --no-cache-dir gunicorn \
    && useradd --create-home acb && mkdir /data && chown acb /data
ENV ACB_DB=/data/acbench.db
# tune without rebuilding: WEB_CONCURRENCY = worker processes (about one per CPU core), threads per worker below
ENV WEB_CONCURRENCY=2 GUNICORN_CMD_ARGS="--worker-class gthread --threads 8 --timeout 30 --access-logfile -"
VOLUME /data
USER acb
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/health', timeout=4).status == 200 else 1)"
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "server:application"]
