"""
Database Utility Functions

Common utility functions for database operations across the application.
"""

import os
import logging
from typing import Dict, Optional, Any
from dataclasses import dataclass

from src.config.env import warehouse_path

logger = logging.getLogger(__name__)


@dataclass
class DatabaseConfig:
    """Database configuration parameters."""
    host: str
    port: int
    database: str
    username: str
    password: str
    ssl_mode: str = "require"


def get_postgres_connection_params() -> Dict[str, Any]:
    """Get PostgreSQL connection parameters from environment variables."""
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "database": os.getenv("POSTGRES_DATABASE", "neuronews"),
        "username": os.getenv("POSTGRES_USERNAME", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", "password"),
        "ssl_mode": os.getenv("POSTGRES_SSL_MODE", "prefer")
    }


def get_duckdb_path() -> str:
    """Return the path to the local DuckDB warehouse file."""
    return warehouse_path("data/neuronews.duckdb") or "data/neuronews.duckdb"


def format_connection_string(params: Dict[str, Any], db_type: str = "postgresql") -> str:
    """Format connection parameters into a connection string."""
    if db_type in ("postgresql", "postgres"):
        return (
            f"postgresql://{params['username']}:{params['password']}"
            f"@{params['host']}:{params['port']}/{params['database']}"
            f"?sslmode={params.get('ssl_mode', 'prefer')}"
        )
    elif db_type == "duckdb":
        return get_duckdb_path()
    else:
        raise ValueError(f"Unsupported database type: {db_type}")


def validate_connection_params(params: Dict[str, Any], required_keys: list) -> bool:
    """Validate that required connection parameters are present."""
    missing_keys = []
    for key in required_keys:
        if key not in params or not params[key]:
            missing_keys.append(key)
    
    if missing_keys:
        logger.error(f"Missing required connection parameters: {missing_keys}")
        return False
    
    return True


def create_database_config(db_type: str) -> Optional[DatabaseConfig]:
    """Create a DatabaseConfig object for the specified database type."""
    try:
        if db_type == "postgres":
            params = get_postgres_connection_params()
        else:
            logger.error(f"Unsupported database type for DatabaseConfig: {db_type}")
            return None
        
        return DatabaseConfig(
            host=params["host"],
            port=params["port"],
            database=params["database"],
            username=params["username"],
            password=params["password"],
            ssl_mode=params.get("ssl_mode", "prefer")
        )
    except Exception as e:
        logger.error(f"Error creating database config: {e}")
        return None


def sanitize_table_name(table_name: str) -> str:
    """Sanitize table name to prevent SQL injection."""
    # Remove any characters that aren't alphanumeric, underscore, or dot
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_.]', '', table_name)
    
    # Ensure it doesn't start with a number
    if sanitized and sanitized[0].isdigit():
        sanitized = f"t_{sanitized}"
    
    return sanitized


def build_where_clause(conditions: Dict[str, Any]) -> str:
    """Build a WHERE clause from a dictionary of conditions."""
    if not conditions:
        return ""
    
    clauses = []
    for key, value in conditions.items():
        sanitized_key = sanitize_table_name(key)
        if isinstance(value, str):
            clauses.append(f"{sanitized_key} = '{value}'")
        elif isinstance(value, (int, float)):
            clauses.append(f"{sanitized_key} = {value}")
        elif isinstance(value, list):
            values_str = "', '".join(str(v) for v in value)
            clauses.append(f"{sanitized_key} IN ('{values_str}')")
    
    return " AND ".join(clauses)
