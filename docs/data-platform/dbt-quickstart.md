# dbt Quickstart Guide for NeuroNews

This guide will help you get started with the dbt analytics project for NeuroNews.

## Prerequisites

- Python 3.8+
- dbt-core installed
- dbt-duckdb adapter for local development
- dbt-snowflake adapter for production (optional)

## Installation

1. **Install dbt and required adapters:**
   ```bash
   pip install dbt-core dbt-duckdb dbt-snowflake
   ```

2. **Install dbt packages:**
   ```bash
   make dbt-deps
   ```

## Local Development (DuckDB)

The project is configured to use DuckDB by default for local development:

1. **Build all models:**
   ```bash
   make dbt-build
   ```

2. **Run tests:**
   ```bash
   make dbt-test
   ```

3. **Generate and view documentation:**
   ```bash
   make dbt-docs
   ```

## Project Structure

```
dbt/
├── profiles.yml              # Database connection configuration
└── neuro_news/
    ├── dbt_project.yml       # Project configuration
    ├── packages.yml          # External package dependencies
    ├── models/
    │   ├── staging/          # Data cleaning and standardization
    │   │   ├── _sources.yml  # Source table definitions
    │   │   └── stg_postgres__articles.sql
    │   └── marts/            # Business logic and final tables
    │       └── fct_articles.sql
    ├── macros/               # Reusable SQL macros
    ├── tests/                # Custom data tests
    └── seeds/                # Static lookup data
```

## Database Configuration

### Local Development (DuckDB)
- Database file: `./dbt/neuro_news.duckdb`
- No additional setup required
- Perfect for development and testing

### Production (Snowflake)
To switch to Snowflake for production:

1. **Uncomment Snowflake configuration in `dbt/profiles.yml`**

2. **Set environment variables:**
   ```bash
   export SNOWFLAKE_ACCOUNT=your-account.snowflakecomputing.com
   export SNOWFLAKE_USER=your-username
   export SNOWFLAKE_PASSWORD=your-password
   export SNOWFLAKE_ROLE=ANALYTICS_ROLE
   export SNOWFLAKE_DATABASE=NEURONEWS
   export SNOWFLAKE_WAREHOUSE=ANALYTICS_WH
   export SNOWFLAKE_SCHEMA=ANALYTICS
   ```

3. **Change target in profiles.yml:**
   ```yaml
   neuro_news:
     target: prod  # Changed from 'dev'
   ```

## Available Make Commands

- `make dbt-deps` - Install dbt packages
- `make dbt-build` - Build all models and run tests
- `make dbt-test` - Run all tests
- `make dbt-docs` - Generate and serve documentation
- `make dbt-clean` - Clean generated artifacts

## Included Packages

- **dbt-utils**: Utility macros for common transformations
- **dbt-expectations**: Advanced data quality testing framework

## Data Sources

Currently configured sources:
- **postgres.articles**: Main articles table from PostgreSQL

## Models

### Staging Layer
- `stg_postgres__articles`: Cleaned and standardized articles data

### Marts Layer  
- `fct_articles`: Enriched articles fact table with derived metrics

## Getting Help

- [dbt Documentation](https://docs.getdbt.com/)
- [dbt-utils Package](https://github.com/dbt-labs/dbt-utils)
- [dbt-expectations Package](https://github.com/calogica/dbt-expectations)

## Next Steps

1. Connect to your PostgreSQL source data
2. Customize the staging models for your specific data schema
3. Add business logic in the marts layer
4. Implement data quality tests
5. Set up production deployment with Snowflake
