FROM mcr.microsoft.com/mssql/server:2022-latest

USER root

# Copy init script and entrypoint
COPY init.sql /init.sql
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER mssql

ENTRYPOINT ["/entrypoint.sh"]
