#!/usr/bin/env bash
# Runs once, on first initialisation of the PostgreSQL data volume.
# - Enables pgvector in the application database as the superuser.
# - Creates a separate database for integration tests.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE DATABASE "${POSTGRES_DB}_test" OWNER "$POSTGRES_USER";
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${POSTGRES_DB}_test" <<-SQL
    CREATE EXTENSION IF NOT EXISTS vector;
SQL
