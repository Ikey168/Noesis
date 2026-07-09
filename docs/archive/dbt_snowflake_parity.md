# dbt Snowflake Parity Configuration

This document describes how to configure and switch between DuckDB (local development) and Snowflake (production) environments for the NeuroNews dbt project.

## Overview

The `dbt/profiles.yml` file contains multiple target configurations to support different deployment scenarios:

- **`dev`** (default): DuckDB for local development
- **`prod`**: Snowflake with password authentication
- **`prod_iam`**: Snowflake with IAM/SSO authentication
- **`prod_keypair`**: Snowflake with key-pair authentication (recommended for CI/CD)
- **`prod_oauth`**: Snowflake with OAuth browser authentication
- **`s3_external`**: Snowflake with S3 external tables support

## Environment Variables

### Required Core Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SNOWFLAKE_ACCOUNT` | Snowflake account identifier | `your-account.snowflakecomputing.com` |
| `SNOWFLAKE_USER` | Snowflake username | `analytics_user` |
| `SNOWFLAKE_ROLE` | Snowflake role (optional, defaults to ANALYTICS_ROLE) | `ANALYTICS_ROLE` |
| `SNOWFLAKE_DATABASE` | Target database (optional, defaults to NEURONEWS) | `NEURONEWS` |
| `SNOWFLAKE_WAREHOUSE` | Compute warehouse (optional, defaults to ANALYTICS_WH) | `ANALYTICS_WH` |
| `SNOWFLAKE_SCHEMA` | Target schema (optional, defaults to ANALYTICS) | `ANALYTICS` |

### Authentication Variables

#### Password Authentication
```bash
export SNOWFLAKE_PASSWORD="your-secure-password"
```

#### Key-Pair Authentication (Recommended for CI/CD)
```bash
export SNOWFLAKE_PRIVATE_KEY_PATH="/path/to/rsa_key.p8"
```

#### IAM/SSO Authentication
No additional variables needed - uses `authenticator: externalbrowser`

### S3 External Schema Variables (Optional)

| Variable | Description | Example |
|----------|-------------|---------|
| `SNOWFLAKE_S3_STAGE_URL` | S3 bucket URL for external data | `s3://neuronews-data/raw/` |
| `SNOWFLAKE_S3_AWS_ROLE` | AWS IAM role for Snowflake integration | `arn:aws:iam::123456789012:role/SnowflakeIntegrationRole` |
| `SNOWFLAKE_S3_EXTERNAL_ID` | External ID for AWS role assumption | `unique-external-id-123` |

## Usage Examples

### Local Development (Default)
```bash
# Uses DuckDB by default
dbt build --profiles-dir ../
```

### Snowflake Production
```bash
# Set environment variables
export SNOWFLAKE_ACCOUNT="your-account.snowflakecomputing.com"
export SNOWFLAKE_USER="analytics_user"
export SNOWFLAKE_PASSWORD="your-password"

# Uncomment the 'prod' target in profiles.yml, then run:
dbt build --profiles-dir ../ --target prod
```

### Snowflake with Key-Pair Authentication
```bash
# Set environment variables
export SNOWFLAKE_ACCOUNT="your-account.snowflakecomputing.com"
export SNOWFLAKE_USER="analytics_user"
export SNOWFLAKE_PRIVATE_KEY_PATH="/path/to/rsa_key.p8"

# Uncomment the 'prod_keypair' target in profiles.yml, then run:
dbt build --profiles-dir ../ --target prod_keypair
```

## Makefile Integration

### Available Targets

- `make dbt-build` - Build with default target (DuckDB)
- `make dbt-build-snowflake` - Build with Snowflake target (no-op locally)

### Snowflake Build Target

The `dbt-build-snowflake` target is designed for CI/CD environments where Snowflake credentials are available:

```makefile
dbt-build-snowflake:
	@echo "🏔️  Building dbt models on Snowflake..."
	@if [ -z "$$SNOWFLAKE_ACCOUNT" ]; then \
		echo "⚠️  SNOWFLAKE_ACCOUNT not set - skipping Snowflake build"; \
		echo "ℹ️  This is expected in local development"; \
	else \
		cd dbt/neuro_news && dbt build --profiles-dir ../ --target prod; \
	fi
```

## Setup Instructions

### 1. Configure Environment Variables

Create a `.env` file (do not commit to version control):

```bash
# .env
SNOWFLAKE_ACCOUNT=your-account.snowflakecomputing.com
SNOWFLAKE_USER=analytics_user
SNOWFLAKE_PASSWORD=your-secure-password
SNOWFLAKE_ROLE=ANALYTICS_ROLE
SNOWFLAKE_DATABASE=NEURONEWS
SNOWFLAKE_WAREHOUSE=ANALYTICS_WH
SNOWFLAKE_SCHEMA=ANALYTICS
```

### 2. Uncomment Target Configuration

In `dbt/profiles.yml`, uncomment the desired Snowflake target:

```yaml
neuro_news:
  target: prod  # Change from 'dev' to 'prod'
  outputs:
    # ... existing configurations
    
    prod:  # Uncomment this section
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      # ... rest of configuration
```

### 3. Validate Configuration

```bash
cd dbt/neuro_news
dbt debug --profiles-dir ../
```

Expected output:
```
✅ Connection test: [OK connection ok]
✅ All checks passed!
```

### 4. Run dbt Build

```bash
dbt build --profiles-dir ../
```

## CI/CD Integration

### GitHub Actions Example

```yaml
- name: Set Snowflake Environment Variables
  env:
    SNOWFLAKE_ACCOUNT: ${{ secrets.SNOWFLAKE_ACCOUNT }}
    SNOWFLAKE_USER: ${{ secrets.SNOWFLAKE_USER }}
    SNOWFLAKE_PRIVATE_KEY_PATH: ${{ secrets.SNOWFLAKE_PRIVATE_KEY_PATH }}
  run: |
    cd dbt/neuro_news
    dbt build --profiles-dir ../ --target prod_keypair
```

### Required Secrets

Store these in your CI/CD platform's secrets management:

- `SNOWFLAKE_ACCOUNT`
- `SNOWFLAKE_USER`
- `SNOWFLAKE_PRIVATE_KEY_PATH` (or private key content)
- `SNOWFLAKE_ROLE` (optional)
- `SNOWFLAKE_DATABASE` (optional)
- `SNOWFLAKE_WAREHOUSE` (optional)

## Troubleshooting

### Common Issues

1. **Connection timeout**: Check warehouse is running and scaled appropriately
2. **Permission denied**: Verify role has necessary privileges on database/schema
3. **Invalid account**: Ensure account identifier includes region (e.g., `.us-east-1`)

### Debug Commands

```bash
# Test connection
dbt debug --profiles-dir ../ --target prod

# List available targets
dbt list --profiles-dir ../ --target prod

# Test with specific model
dbt run --profiles-dir ../ --target prod --select dim_source
```

## Security Best Practices

1. **Never commit credentials** to version control
2. **Use key-pair authentication** for production/CI environments
3. **Rotate keys regularly** and use strong passwords
4. **Limit role permissions** to only required databases/schemas
5. **Monitor query usage** and costs in Snowflake console

## Migration Considerations

When switching from DuckDB to Snowflake:

1. **Data types** may need adjustment (e.g., `TIMESTAMP WITH TIME ZONE` → `TIMESTAMP_TZ`)
2. **Function syntax** differences (DuckDB vs Snowflake SQL)
3. **Performance** characteristics vary between engines
4. **Cost management** - Snowflake charges for compute time

For detailed migration steps, refer to the [dbt Snowflake adapter documentation](https://docs.getdbt.com/reference/warehouse-setups/snowflake-setup).
