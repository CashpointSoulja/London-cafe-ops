FROM nousresearch/hermes-agent:latest

USER root
COPY entrypoint.sh /opt/hermes-cloud/entrypoint.sh
COPY scripts /opt/hermes-cloud/scripts
COPY plugins/corgi-revenue /opt/hermes-cloud/plugins/corgi-revenue
RUN chmod 700 /opt/hermes-cloud/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/opt/hermes-cloud/entrypoint.sh"]
