FROM python:3.13-alpine

RUN addgroup -S aether && adduser -S -G aether -u 10001 aether
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --requirement /app/requirements.txt
COPY aether_field_api.py /app/aether_field_api.py
COPY chirpstack_bridge.py /app/chirpstack_bridge.py
RUN mkdir -p /data/media && chown -R aether:aether /data

USER aether
EXPOSE 9443
VOLUME ["/data"]

HEALTHCHECK NONE
CMD ["python", "/app/aether_field_api.py"]
