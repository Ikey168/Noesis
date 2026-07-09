# MLflow Security & Access Control

This document covers security hardening and access control for MLflow deployments in the NeuroNews project, from development environments to production considerations.

## Overview

MLflow by default runs without authentication, making it suitable for development but requiring additional security measures for production use. This guide provides:

- Local development protection with reverse proxy
- Production security considerations and patterns
- Backup strategies for MLflow data
- Access control best practices

## Development Environment Security

### Reverse Proxy with Basic Authentication

For local development environments, we provide a reverse proxy configuration that adds basic authentication to MLflow access.

**Benefits:**
- Simple authentication for development teams
- Protection against accidental exposure
- Easy to configure and maintain
- Compatible with existing MLflow setup

**Limitations:**
- Basic auth is not suitable for production
- No user management or role-based access
- Credentials stored in plain text

### Setup Instructions

1. **Install nginx** (if not already installed):
   ```bash
   # Ubuntu/Debian
   sudo apt update && sudo apt install nginx apache2-utils
   
   # macOS with Homebrew
   brew install nginx
   
   # CentOS/RHEL
   sudo yum install nginx httpd-tools
   ```

2. **Create password file**:
   ```bash
   # Create directory for auth files
   sudo mkdir -p /etc/nginx/auth
   
   # Create user credentials (replace 'mlflow-user' with desired username)
   sudo htpasswd -c /etc/nginx/auth/mlflow.htpasswd mlflow-user
   
   # Add additional users (without -c flag)
   sudo htpasswd /etc/nginx/auth/mlflow.htpasswd another-user
   ```

3. **Configure nginx** using the provided configuration file:
   ```bash
   # Copy our configuration
   sudo cp infra/reverse_proxy/mlflow-nginx.conf /etc/nginx/sites-available/mlflow
   
   # Enable the site
   sudo ln -s /etc/nginx/sites-available/mlflow /etc/nginx/sites-enabled/
   
   # Test configuration
   sudo nginx -t
   
   # Restart nginx
   sudo systemctl restart nginx
   ```

4. **Start MLflow** on the backend port:
   ```bash
   # Start MLflow on localhost only (not accessible externally)
   mlflow server --host 127.0.0.1 --port 5000 --backend-store-uri sqlite:///mlflow.db
   ```

5. **Access MLflow** through the proxy:
   - URL: `http://localhost:8080/mlflow/`
   - Username/Password: As configured in step 2

## Production Security Considerations

### Authentication & Authorization

For production deployments, consider these robust authentication patterns:

#### 1. OAuth Integration
```yaml
# Example with OAuth2 Proxy
oauth2-proxy:
  image: quay.io/oauth2-proxy/oauth2-proxy:v7.4.0
  environment:
    - OAUTH2_PROXY_PROVIDER=github
    - OAUTH2_PROXY_CLIENT_ID=your-github-app-id
    - OAUTH2_PROXY_CLIENT_SECRET=your-github-app-secret
    - OAUTH2_PROXY_UPSTREAM=http://mlflow:5000
    - OAUTH2_PROXY_EMAIL_DOMAINS=yourcompany.com
```

#### 2. Enterprise SSO Integration
- **SAML**: Using tools like KeyCloak or Auth0
- **LDAP**: For corporate directory integration
- **JWT**: For microservice architectures

#### 3. Cloud Provider Integration
- **AWS**: ALB with AWS Cognito
- **GCP**: Cloud IAP (Identity-Aware Proxy)
- **Azure**: Application Gateway with Azure AD

### Network Security

#### 1. Network Isolation
```yaml
# Docker Compose example
networks:
  mlflow-internal:
    driver: bridge
    internal: true
  mlflow-external:
    driver: bridge

services:
  mlflow:
    networks:
      - mlflow-internal
  
  reverse-proxy:
    networks:
      - mlflow-internal
      - mlflow-external
    ports:
      - "443:443"
```

#### 2. TLS/SSL Configuration
```nginx
# Production nginx configuration
server {
    listen 443 ssl http2;
    server_name mlflow.yourcompany.com;
    
    ssl_certificate /path/to/certificate.crt;
    ssl_certificate_key /path/to/private.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512;
    ssl_prefer_server_ciphers off;
    
    location / {
        proxy_pass http://mlflow-backend:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### 3. IP Allowlisting
```nginx
# Restrict access by IP range
location / {
    allow 10.0.0.0/8;      # Internal network
    allow 192.168.0.0/16;  # Private network
    deny all;
    
    proxy_pass http://mlflow-backend:5000;
}
```

### Database Security

#### 1. Database Authentication
```python
# Secure database connection
MLFLOW_BACKEND_STORE_URI = "postgresql://mlflow_user:secure_password@db:5432/mlflow_db?sslmode=require"
```

#### 2. Connection Encryption
```yaml
# PostgreSQL with SSL
postgres:
  environment:
    - POSTGRES_SSL_MODE=require
    - POSTGRES_SSL_CERT=/certs/server.crt
    - POSTGRES_SSL_KEY=/certs/server.key
```

### Artifact Storage Security

#### 1. S3 Security
```python
# IAM roles and bucket policies
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::ACCOUNT:role/MLflowRole"},
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": "arn:aws:s3:::mlflow-artifacts/*"
        }
    ]
}
```

#### 2. Encryption at Rest
```bash
# S3 server-side encryption
aws s3api put-bucket-encryption \
  --bucket mlflow-artifacts \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'
```

## Backup & Recovery

### Database Backups

#### 1. PostgreSQL Backup Script
```bash
#!/bin/bash
# mlflow-backup.sh

# Configuration
DB_HOST="localhost"
DB_PORT="5432"
DB_NAME="mlflow"
DB_USER="mlflow_user"
BACKUP_DIR="/backups/mlflow"
RETENTION_DAYS=30

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Create backup with timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/mlflow_db_$TIMESTAMP.sql"

# Perform backup
pg_dump -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME > "$BACKUP_FILE"

# Compress backup
gzip "$BACKUP_FILE"

# Clean old backups
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +$RETENTION_DAYS -delete

echo "Backup completed: ${BACKUP_FILE}.gz"
```

#### 2. Automated Backup with Cron
```bash
# Add to crontab: backup every 6 hours
0 */6 * * * /path/to/mlflow-backup.sh >> /var/log/mlflow-backup.log 2>&1
```

### Artifact Storage Backups

#### 1. File System Artifacts
```bash
#!/bin/bash
# mlruns-backup.sh

MLRUNS_DIR="/path/to/mlruns"
BACKUP_DIR="/backups/mlruns"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Create incremental backup
rsync -av --link-dest="$BACKUP_DIR/latest" \
  "$MLRUNS_DIR/" \
  "$BACKUP_DIR/$TIMESTAMP/"

# Update latest symlink
rm -f "$BACKUP_DIR/latest"
ln -s "$TIMESTAMP" "$BACKUP_DIR/latest"
```

#### 2. S3 Cross-Region Replication
```json
{
    "Rules": [{
        "ID": "MLflowArtifactReplication",
        "Status": "Enabled",
        "Priority": 1,
        "Filter": {"Prefix": "mlflow-artifacts/"},
        "Destination": {
            "Bucket": "arn:aws:s3:::mlflow-artifacts-backup-region",
            "StorageClass": "STANDARD_IA"
        }
    }]
}
```

### Recovery Procedures

#### 1. Database Recovery
```bash
# Stop MLflow services
docker-compose stop mlflow

# Restore database
gunzip -c /backups/mlflow/mlflow_db_20250825_120000.sql.gz | \
  psql -h localhost -U mlflow_user -d mlflow

# Restart services
docker-compose start mlflow
```

#### 2. Artifact Recovery
```bash
# Restore from backup
rsync -av /backups/mlruns/latest/ /path/to/mlruns/

# Or restore specific backup
rsync -av /backups/mlruns/20250825_120000/ /path/to/mlruns/
```

## Monitoring & Auditing

### Access Logging
```nginx
# Enhanced nginx logging
log_format mlflow_access '$remote_addr - $remote_user [$time_local] '
                        '"$request" $status $body_bytes_sent '
                        '"$http_referer" "$http_user_agent" '
                        '$request_time $upstream_response_time';

access_log /var/log/nginx/mlflow-access.log mlflow_access;
```

### Security Monitoring
```bash
# Monitor failed authentication attempts
grep "401\|403" /var/log/nginx/mlflow-access.log | \
  awk '{print $1}' | sort | uniq -c | sort -rn
```

### Compliance Considerations

1. **Data Retention**: Configure automated cleanup of old experiments
2. **Audit Trails**: Log all model registry changes
3. **Data Privacy**: Ensure artifact encryption for sensitive data
4. **Access Reviews**: Regular review of user permissions

## Environment-Specific Configurations

### Development
- Basic authentication via reverse proxy
- Local file storage for artifacts
- SQLite database (acceptable for dev)
- Self-signed certificates

### Staging
- OAuth integration
- S3 with encryption
- PostgreSQL with backups
- Valid SSL certificates

### Production
- Enterprise SSO integration
- Multi-region artifact storage
- High-availability database
- WAF and DDoS protection
- Comprehensive monitoring

## Security Checklist

### Initial Setup
- [ ] Change default ports
- [ ] Enable authentication
- [ ] Configure HTTPS/TLS
- [ ] Set up database encryption
- [ ] Configure artifact encryption
- [ ] Implement network isolation

### Ongoing Security
- [ ] Regular security updates
- [ ] Access permission reviews
- [ ] Backup testing
- [ ] Security monitoring
- [ ] Incident response plan
- [ ] Compliance audits

## Troubleshooting

### Common Issues

1. **502 Bad Gateway**: MLflow service not running or wrong port
2. **401 Unauthorized**: Check credentials and auth configuration
3. **Certificate Errors**: Verify SSL certificate and trust chain
4. **Database Connection**: Check database availability and credentials

### Debug Commands
```bash
# Test nginx configuration
sudo nginx -t

# Check MLflow health
curl -I http://localhost:5000/health

# Verify database connection
psql -h localhost -U mlflow_user -d mlflow -c "SELECT 1;"

# Check SSL certificate
openssl s_client -connect mlflow.domain.com:443 -servername mlflow.domain.com
```

## References

- [MLflow Security Documentation](https://mlflow.org/docs/latest/auth/index.html)
- [nginx Security Guide](https://nginx.org/en/docs/http/securing_http.html)
- [OAuth2 Proxy Documentation](https://oauth2-proxy.github.io/oauth2-proxy/)
- [PostgreSQL Security](https://www.postgresql.org/docs/current/security.html)

---

**Note**: This documentation focuses on practical security measures for MLflow deployments. Always consult with your security team and follow organizational security policies when implementing these configurations in production environments.
