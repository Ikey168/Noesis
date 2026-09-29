#!/bin/bash
set -e

# Navigate to application directory
cd /home/ubuntu/neuronews

# Ensure supervisor is running
sudo service supervisor start

# Restart our application
sudo supervisorctl restart neuronews

# Restart Nginx
sudo service nginx restart

# Start background tasks if needed
bash legacy/scripts/run.sh &

# Wait for services to start
sleep 5

# Check if services are running
if ! sudo supervisorctl status neuronews | grep -q "RUNNING"; then
    echo "Error: Application failed to start"
    exit 1
fi

if ! sudo service nginx status | grep -q "active (running)"; then
    echo "Error: Nginx failed to start"
    exit 1
fi

echo "Application started successfully"