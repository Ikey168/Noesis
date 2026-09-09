#!/bin/bash
#
# Airflow Connections & Variables Bootstrap Script (Issue #194)
#
# This script sets up Airflow connections and variables for NeuroNews.
# It's designed to be idempotent and can be run multiple times safely.
#
# Usage:
#   ./scripts/airflow_bootstrap.sh [--env-file path/to/.env]
#
# Requirements:
#   - Run inside Airflow API server container after `airflow db migrate`
#   - Environment variables for AWS credentials (optional)
#   - Airflow CLI available
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
ENV_FILE=""
VERBOSE=false

# Function to print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to show usage
usage() {
    cat << EOF
Airflow Connections & Variables Bootstrap Script

Usage: $0 [OPTIONS]

Options:
    --env-file PATH     Path to environment file (optional)
    --verbose          Enable verbose output
    -h, --help         Show this help message

Description:
    This script bootstraps Airflow with necessary connections and variables
    for the NeuroNews project. It's idempotent and safe to run multiple times.

Connections Created:
    - aws_default: AWS connection with credentials from environment
    - neuro_postgres: PostgreSQL connection for metadata

Variables Created:
    - DATA_ROOT: Root directory for data files
    - NAMESPACE: OpenLineage namespace for the project
    - ENVIRONMENT: Current environment (dev, staging, prod)

Environment Variables Used:
    - AWS_ACCESS_KEY_ID: AWS access key (optional)
    - AWS_SECRET_ACCESS_KEY: AWS secret key (optional)
    - AWS_DEFAULT_REGION: AWS region (default: us-east-1)
    - POSTGRES_HOST: PostgreSQL host (default: postgres)
    - POSTGRES_PORT: PostgreSQL port (default: 5432)
    - POSTGRES_USER: PostgreSQL user (default: airflow)
    - POSTGRES_PASSWORD: PostgreSQL password (default: airflow)
    - POSTGRES_DB: PostgreSQL database (default: airflow)

Examples:
    # Basic usage (inside Airflow container)
    ./scripts/airflow_bootstrap.sh

    # With custom environment file
    ./scripts/airflow_bootstrap.sh --env-file /path/to/.env

    # With verbose output
    ./scripts/airflow_bootstrap.sh --verbose

EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Load environment file if specified
if [[ -n "$ENV_FILE" ]]; then
    if [[ -f "$ENV_FILE" ]]; then
        print_info "Loading environment from: $ENV_FILE"
        set -a  # automatically export all variables
        source "$ENV_FILE"
        set +a
    else
        print_error "Environment file not found: $ENV_FILE"
        exit 1
    fi
fi

# Function to check if Airflow CLI is available
check_airflow_cli() {
    if ! command -v airflow &> /dev/null; then
        print_error "Airflow CLI not found. This script must run inside an Airflow container."
        exit 1
    fi
    
    # Check if Airflow is initialized
    if ! airflow db check &> /dev/null; then
        print_error "Airflow database not initialized. Run 'airflow db migrate' first."
        exit 1
    fi
    
    print_success "Airflow CLI available and database initialized"
}

# Function to check if connection exists
connection_exists() {
    local conn_id="$1"
    airflow connections get "$conn_id" &> /dev/null
}

# Function to check if variable exists
variable_exists() {
    local var_key="$1"
    airflow variables get "$var_key" &> /dev/null
}

# Function to create or update AWS connection
setup_aws_connection() {
    local conn_id="aws_default"
    
    print_info "Setting up AWS connection: $conn_id"
    
    # Get AWS credentials from environment with defaults
    local aws_access_key="${AWS_ACCESS_KEY_ID:-}"
    local aws_secret_key="${AWS_SECRET_ACCESS_KEY:-}"
    local aws_region="${AWS_DEFAULT_REGION:-us-east-1}"
    
    if [[ -z "$aws_access_key" || -z "$aws_secret_key" ]]; then
        print_warning "AWS credentials not found in environment variables"
        print_warning "Creating placeholder AWS connection (update with real credentials later)"
        aws_access_key="your_aws_access_key_id"
        aws_secret_key="your_aws_secret_access_key"
    fi
    
    # Prepare connection command
    local cmd=(
        airflow connections add "$conn_id"
        --conn-type "aws"
        --conn-login "$aws_access_key"
        --conn-password "$aws_secret_key"
        --conn-extra "{\"region_name\": \"$aws_region\"}"
    )
    
    if connection_exists "$conn_id"; then
        print_info "Connection $conn_id already exists, updating..."
        cmd[2]="add"  # Use add with --overwrite for updates
        cmd+=("--overwrite")
    fi
    
    if [[ "$VERBOSE" == "true" ]]; then
        print_info "Running: ${cmd[*]}"
    fi
    
    if "${cmd[@]}" &> /dev/null; then
        print_success "AWS connection $conn_id configured successfully"
        if [[ "$aws_access_key" == "your_aws_access_key_id" ]]; then
            print_warning "Remember to update $conn_id with real AWS credentials"
        fi
    else
        print_error "Failed to configure AWS connection $conn_id"
        return 1
    fi
}

# Function to create or update PostgreSQL connection
setup_postgres_connection() {
    local conn_id="neuro_postgres"
    
    print_info "Setting up PostgreSQL connection: $conn_id"
    
    # Get PostgreSQL connection details from environment with defaults
    local pg_host="${POSTGRES_HOST:-postgres}"
    local pg_port="${POSTGRES_PORT:-5432}"
    local pg_user="${POSTGRES_USER:-airflow}"
    local pg_password="${POSTGRES_PASSWORD:-airflow}"
    local pg_database="${POSTGRES_DB:-airflow}"
    
    # Prepare connection command
    local cmd=(
        airflow connections add "$conn_id"
        --conn-type "postgres"
        --conn-host "$pg_host"
        --conn-port "$pg_port"
        --conn-login "$pg_user"
        --conn-password "$pg_password"
        --conn-schema "$pg_database"
    )
    
    if connection_exists "$conn_id"; then
        print_info "Connection $conn_id already exists, updating..."
        cmd+=("--overwrite")
    fi
    
    if [[ "$VERBOSE" == "true" ]]; then
        print_info "Running: ${cmd[*]}"
    fi
    
    if "${cmd[@]}" &> /dev/null; then
        print_success "PostgreSQL connection $conn_id configured successfully"
    else
        print_error "Failed to configure PostgreSQL connection $conn_id"
        return 1
    fi
}

# Function to set up Airflow variables
setup_variables() {
    print_info "Setting up Airflow variables..."
    
    # Define variables with their default values
    declare -A variables=(
        ["DATA_ROOT"]="/opt/airflow/data"
        ["NAMESPACE"]="neuro_news_dev"
        ["ENVIRONMENT"]="${ENVIRONMENT:-dev}"
        ["OPENLINEAGE_URL"]="${OPENLINEAGE_URL:-http://marquez:5000}"
        ["OPENLINEAGE_NAMESPACE"]="${OPENLINEAGE_NAMESPACE:-neuro_news_dev}"
        ["PROJECT_VERSION"]="${PROJECT_VERSION:-1.0.0}"
    )
    
    # Set each variable
    for var_key in "${!variables[@]}"; do
        local var_value="${variables[$var_key]}"
        
        if variable_exists "$var_key"; then
            local current_value
            current_value=$(airflow variables get "$var_key" 2>/dev/null || echo "")
            if [[ "$current_value" != "$var_value" ]]; then
                print_info "Variable $var_key exists with different value, updating..."
                if [[ "$VERBOSE" == "true" ]]; then
                    print_info "Current: $current_value -> New: $var_value"
                fi
            else
                print_info "Variable $var_key already set correctly: $var_value"
                continue
            fi
        fi
        
        if airflow variables set "$var_key" "$var_value" &> /dev/null; then
            print_success "Variable $var_key set to: $var_value"
        else
            print_error "Failed to set variable $var_key"
            return 1
        fi
    done
}

# Function to validate connections
validate_connections() {
    print_info "Validating connections..."
    
    local connections=("aws_default" "neuro_postgres")
    local validation_failed=false
    
    for conn_id in "${connections[@]}"; do
        if connection_exists "$conn_id"; then
            print_success "Connection $conn_id exists and is accessible"
            
            # Try to test the connection
            if airflow connections test "$conn_id" &> /dev/null; then
                print_success "Connection $conn_id test successful"
            else
                print_warning "Connection $conn_id exists but test failed (this may be expected for placeholder credentials)"
            fi
        else
            print_error "Connection $conn_id does not exist"
            validation_failed=true
        fi
    done
    
    if [[ "$validation_failed" == "true" ]]; then
        return 1
    fi
}

# Function to validate variables
validate_variables() {
    print_info "Validating variables..."
    
    local variables=("DATA_ROOT" "NAMESPACE" "ENVIRONMENT" "OPENLINEAGE_URL" "OPENLINEAGE_NAMESPACE" "PROJECT_VERSION")
    local validation_failed=false
    
    for var_key in "${variables[@]}"; do
        if variable_exists "$var_key"; then
            local var_value
            var_value=$(airflow variables get "$var_key")
            print_success "Variable $var_key = $var_value"
        else
            print_error "Variable $var_key does not exist"
            validation_failed=true
        fi
    done
    
    if [[ "$validation_failed" == "true" ]]; then
        return 1
    fi
}

# Function to display summary
display_summary() {
    print_info "Bootstrap Summary:"
    echo ""
    echo "Connections:"
    airflow connections list --output table 2>/dev/null | grep -E "(aws_default|neuro_postgres)" || echo "  No NeuroNews connections found"
    echo ""
    echo "Variables:"
    for var in "DATA_ROOT" "NAMESPACE" "ENVIRONMENT" "OPENLINEAGE_URL" "OPENLINEAGE_NAMESPACE" "PROJECT_VERSION"; do
        if variable_exists "$var"; then
            local value
            value=$(airflow variables get "$var" 2>/dev/null)
            printf "  %-20s = %s\n" "$var" "$value"
        fi
    done
    echo ""
}

# Main execution function
main() {
    print_info "Starting Airflow bootstrap for NeuroNews (Issue #194)"
    print_info "=================================================="
    
    # Check prerequisites
    check_airflow_cli
    
    # Set up connections
    setup_aws_connection
    setup_postgres_connection
    
    # Set up variables
    setup_variables
    
    # Validate everything was set up correctly
    validate_connections
    validate_variables
    
    # Display summary
    display_summary
    
    print_success "Airflow bootstrap completed successfully!"
    print_info "You can now run your DAGs with properly configured connections and variables."
    
    # Check for placeholder credentials
    if airflow connections get aws_default 2>/dev/null | grep -q "your_aws_access_key_id"; then
        echo ""
        print_warning "⚠️  AWS connection is using placeholder credentials"
        print_info "To update with real credentials, run:"
        print_info "airflow connections add aws_default --conn-type aws --conn-login YOUR_KEY --conn-password YOUR_SECRET --overwrite"
    fi
}

# Error handling
trap 'print_error "Script failed at line $LINENO"' ERR

# Run main function
main "$@"
