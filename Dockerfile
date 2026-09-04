FROM nousresearch/hermes-agent:latest

USER root
COPY entrypoint.sh /opt/hermes-cloud/entrypoint.sh
COPY deterministic_bot.py /opt/hermes-cloud/deterministic_bot.py
COPY scripts /opt/hermes-cloud/scripts
RUN chmod 700 /opt/hermes-cloud/entrypoint.sh /opt/hermes-cloud/deterministic_bot.py

EXPOSE 8080
ENTRYPOINT ["/opt/hermes-cloud/entrypoint.sh"]
