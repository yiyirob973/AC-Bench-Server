# Build from the ac_bench folder (the parent of cloud_server):
#   docker build -f cloud_server/Dockerfile -t acbench-server .
#   docker run -d --name acbench -p 127.0.0.1:8080:8080 -v acbench-data:/data acbench-server
# Put a TLS reverse proxy (Caddy / nginx) in front; never expose port 8080 directly.
FROM python:3.12-slim
WORKDIR /app
COPY ac_rating.py ac_car_classifier.py /app/
COPY cloud_server/server.py /app/server.py
RUN pip install --no-cache-dir gunicorn \
 && useradd --create-home acb && mkdir /data && chown acb /data
ENV ACB_DB=/data/acbench.db
VOLUME /data
USER acb
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/health', timeout=4).status == 200 else 1)"
CMD ["gunicorn", "--workers", "2", "--timeout", "30", "--access-logfile", "-", "--bind", "0.0.0.0:8080", "server:application"]
