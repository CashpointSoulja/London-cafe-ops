FROM nousresearch/hermes-agent:latest

USER root
COPY entrypoint.sh /opt/hermes-cloud/entrypoint.sh
RUN chmod 700 /opt/hermes-cloud/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/opt/hermes-cloud/entrypoint.sh"]
