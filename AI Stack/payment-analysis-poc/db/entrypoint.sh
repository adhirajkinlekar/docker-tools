#!/bin/bash
set -e

SA_PASS="${SA_PASSWORD:-YourStrong@Passw0rd}"

# Start SQL Server in the background
/opt/mssql/bin/sqlservr &
SQL_PID=$!

echo "[db-init] SQL Server starting (PID $SQL_PID) …"

# Wait until SQL Server accepts connections (up to 60 s)
for i in $(seq 1 30); do
    /opt/mssql-tools18/bin/sqlcmd \
        -S localhost -U sa -P "$SA_PASS" \
        -Q "SELECT 1" -C -N \
        > /dev/null 2>&1 && break
    echo "[db-init] Waiting for SQL Server … ($i/30)"
    sleep 2
done

echo "[db-init] Running init.sql …"
/opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "$SA_PASS" \
    -i /init.sql -C -N \
    && echo "[db-init] Database initialised successfully." \
    || echo "[db-init] WARNING: init.sql had errors (may already be initialised)."

# Hand off to SQL Server process
wait $SQL_PID
