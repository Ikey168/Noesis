# MLflow Reverse Proxy Setup

This directory contains configuration and scripts for setting up a secure reverse proxy for MLflow access as part of **Issue #226: Hardening & access**.

## Overview

The setup provides:
- nginx reverse proxy with basic authentication
- Secure access to MLflow development environment
- Rate limiting and security headers
- SSL/TLS support for production use
- Automated setup scripts

## Quick Start

### 1. Automated Setup (Recommended)

```bash
# Run the setup script
cd infra/reverse_proxy
./setup_mlflow_security.sh
```

This script will:
- Install required dependencies (nginx, apache2-utils)
- Create authentication directory and users
- Install nginx configuration
- Create MLflow startup script
- Restart nginx service

### 2. Manual Setup

If you prefer manual setup or need customization:

1. **Install dependencies:**
   ```bash
   sudo apt update
   sudo apt install nginx apache2-utils
   ```

2. **Create auth directory:**
   ```bash
   sudo mkdir -p /etc/nginx/auth
   ```

3. **Create auth users:**
   ```bash
   sudo htpasswd -c /etc/nginx/auth/mlflow.htpasswd admin
   ```

4. **Install nginx config:**
   ```bash
   sudo cp mlflow-nginx.conf /etc/nginx/sites-available/mlflow
   sudo ln -s /etc/nginx/sites-available/mlflow /etc/nginx/sites-enabled/
   ```

5. **Test and restart nginx:**
   ```bash
   sudo nginx -t
   sudo systemctl restart nginx
   ```

## Usage

### Starting MLflow

Use the generated startup script for secure configuration:

```bash
# From project root
./start_mlflow_secure.sh
```

Or start manually with localhost-only binding:

```bash
mlflow server \
    --host 127.0.0.1 \
    --port 5000 \
    --backend-store-uri sqlite:///mlflow.db \
    --default-artifact-root ./mlruns
```

### Accessing MLflow

- **URL:** http://localhost:8080/mlflow/
- **Authentication:** Use the username/password created during setup
- **Direct access blocked:** MLflow only accepts connections from localhost

## Configuration Files

### `mlflow-nginx.conf`
Main nginx configuration providing:
- Reverse proxy to MLflow backend
- Basic HTTP authentication
- Rate limiting (100 req/min per IP)
- Security headers
- WebSocket support for real-time features
- SSL configuration (commented, for production)

### `setup_mlflow_security.sh`
Automated setup script that:
- Checks dependencies
- Creates auth directory and users
- Installs nginx configuration
- Creates MLflow startup script
- Provides setup verification

## Security Features

### Development Environment
- Basic HTTP authentication
- Rate limiting protection
- Security headers (XSS, CSRF, etc.)
- Localhost-only MLflow binding
- Access logging

### Production Considerations
The configuration includes commented sections for:
- SSL/TLS termination
- Enhanced rate limiting
- Additional security headers
- Monitoring integration

## Troubleshooting

### Check nginx status
```bash
sudo systemctl status nginx
```

### View logs
```bash
# Access logs
sudo tail -f /var/log/nginx/mlflow-access.log

# Error logs
sudo tail -f /var/log/nginx/mlflow-error.log

# nginx error log
sudo tail -f /var/log/nginx/error.log
```

### Test configuration
```bash
sudo nginx -t
```

### Add more users
```bash
sudo htpasswd /etc/nginx/auth/mlflow.htpasswd newuser
```

### Remove a user
```bash
sudo htpasswd -D /etc/nginx/auth/mlflow.htpasswd username
```

## Port Configuration

- **8080:** nginx reverse proxy (public access)
- **5000:** MLflow backend (localhost only)

If port 8080 conflicts with other services, modify the `listen 8080;` line in `mlflow-nginx.conf`.

## Advanced Configuration

### Custom Backend Store

Set environment variables before starting MLflow:

```bash
export MLFLOW_BACKEND_STORE_URI="postgresql://user:pass@localhost/mlflow"
export MLFLOW_ARTIFACT_ROOT="s3://my-bucket/artifacts"
./start_mlflow_secure.sh
```

### SSL Certificate Setup

For production with SSL:

1. Obtain SSL certificates
2. Uncomment SSL sections in nginx config
3. Update certificate paths
4. Change listen port to 443

### Integration with Docker

For containerized deployment, see the main project's Docker configuration and adapt the nginx setup accordingly.

## Security Notes

⚠️ **Important Security Considerations:**

1. **Change default passwords** immediately after setup
2. **Use strong passwords** for all accounts
3. **Monitor access logs** regularly
4. **Keep nginx updated** for security patches
5. **For production:** Implement proper SSL/TLS and consider enterprise authentication

## Related Documentation

- [MLflow Security Guide](../../docs/subsystems/mlops/security.md) - Comprehensive security documentation
- [Project Docker Setup](../../docker-compose.yml) - Container configuration
- [MLflow Official Docs](https://mlflow.org/docs/latest/index.html) - MLflow documentation

## Support

For issues related to this setup:
1. Check the troubleshooting section above
2. Review nginx error logs
3. Verify MLflow is running on localhost:5000
4. Ensure authentication file exists and is readable

## License

This configuration is part of the NeuroNews project and follows the same license terms.
