FROM nousresearch/hermes-agent:latest

USER root
COPY entrypoint.sh /opt/hermes-cloud/entrypoint.sh
COPY scripts/square_daily_revenue.py /opt/hermes-cloud/square_daily_revenue.py
COPY scripts/revenue_summary.py /opt/hermes-cloud/revenue_summary.py
RUN chown hermes:hermes /opt/hermes-cloud/*.py \
    && chmod 755 /opt/hermes-cloud/entrypoint.sh /opt/hermes-cloud/*.py

EXPOSE 8080
ENTRYPOINT ["/opt/hermes-cloud/entrypoint.sh"]
