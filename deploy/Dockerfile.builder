FROM eclipse-temurin:17-jdk

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*
