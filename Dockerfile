FROM python:3.12-slim

USER root
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /opt/hermes-cloud/entrypoint.sh
COPY deterministic_bot.py /opt/hermes-cloud/deterministic_bot.py
COPY scripts /opt/hermes-cloud/scripts
WORKDIR /opt/hermes-cloud
RUN chmod 700 /opt/hermes-cloud/entrypoint.sh /opt/hermes-cloud/deterministic_bot.py

EXPOSE 8080
ENTRYPOINT ["/opt/hermes-cloud/entrypoint.sh"]
