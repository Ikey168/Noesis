# Disaster Recovery Testing Suite

This directory contains scripts and tools for testing the disaster recovery capabilities of the NeuroNews system, focusing on **Redshift** and **Neptune** database resilience with automated recovery.

## 🎯 Objective

Test the complete disaster recovery plan to ensure **database resilience with automated recovery** for both Redshift (data warehouse) and Neptune (graph database) components.

## 📋 Test Coverage

### ✅ Completed Tasks

- [x] **Redshift Disaster Recovery**

  - Simulate Redshift failure & restore from snapshot

  - Automated snapshot restore and endpoint switching

  - Cleanup and rollback functionality

- [x] **Neptune Replica Failover**

  - Query Neptune replica instead of primary instance

  - Endpoint configuration management

  - Performance comparison between primary and replica

- [x] **Comprehensive Testing Suite**

  - End-to-end disaster recovery scenarios

  - Configuration management validation

  - Automated reporting and logging

## 🔧 Scripts Overview

### Core Testing Scripts

| Script | Purpose | Key Features |
|--------|---------|--------------|
| `test_disaster_recovery.sh` | Main test orchestrator | Runs all DR tests, generates reports |
| `test_redshift_dr.sh` | Redshift disaster recovery | Snapshot restore, failover simulation |
| `test_neptune_failover.sh` | Neptune replica testing | Replica connectivity, performance testing |

### Configuration Management

| Script | Purpose | Key Features |
|--------|---------|--------------|
| `manage_db_endpoints.py` | Database endpoint manager | Multi-database endpoint switching |
| `neptune_failover_manager.py` | Neptune-specific failover | Replica switching, rollback capability |

## 🚀 Quick Start

### Prerequisites

```bash

# Required tools

- AWS CLI (configured with appropriate permissions)

- Python 3.x

- jq (JSON processor)

- nc (netcat)

- timeout

# Required permissions

- Redshift: describe-clusters, describe-cluster-snapshots, restore-from-cluster-snapshot

- Neptune: describe-db-clusters, describe-db-instances

- EC2: describe-security-groups, authorize/revoke-security-group-ingress

```text

### Running Tests

#### 1. Complete Test Suite

```bash

# Run all disaster recovery tests

./scripts/test_disaster_recovery.sh

# With custom cluster identifiers

./scripts/test_disaster_recovery.sh \
  --redshift-cluster "my-redshift-cluster" \
  --neptune-cluster "my-neptune-cluster" \
  --region "us-west-2"

```text

#### 2. Individual Component Tests

**Redshift Disaster Recovery:**

```bash

export REDSHIFT_CLUSTER_IDENTIFIER="news-cluster-dev"
export AWS_REGION="us-east-1"
./scripts/test_redshift_dr.sh

```text

**Neptune Replica Failover:**

```bash

export NEPTUNE_CLUSTER_IDENTIFIER="neuronews-dev"
export AWS_REGION="us-east-1"
./scripts/test_neptune_failover.sh

```text

#### 3. Configuration Management

**Switch to Neptune Replica:**

```bash

# Switch application to use replica endpoint

python3 scripts/neptune_failover_manager.py switch-to-replica \
  --replica-endpoint "ws://replica-endpoint:8182/gremlin"

# Check current configuration

python3 scripts/neptune_failover_manager.py status

# Switch back to primary

python3 scripts/neptune_failover_manager.py switch-to-primary

```text

**Multi-database Endpoint Management:**

```bash

# Update both Redshift and Neptune endpoints

python3 scripts/manage_db_endpoints.py \
  --redshift-cluster "backup-cluster" \
  --neptune-cluster "neptune-cluster" \
  --use-replica \
  --region "us-east-1"

```text

## 📊 Test Reports

The test suite generates comprehensive reports:

```bash

# Test logs and reports are stored in:

logs/dr-tests/
├── disaster_recovery_report_YYYYMMDD_HHMMSS.json  # Main test report

├── test.log                                       # Combined test log

├── redshift_dr_test.log                          # Redshift test details

├── neptune_failover_test.log                     # Neptune test details

└── config_management_test.log                    # Config test details

```text

### Sample Report Structure

```json

{
  "disaster_recovery_test": {
    "started_at": "2025-06-07T00:00:00Z",
    "completed_at": "2025-06-07T00:15:00Z",
    "test_environment": {
      "redshift_cluster": "news-cluster-dev",
      "neptune_cluster": "neuronews-dev",
      "aws_region": "us-east-1"
    },
    "tests": {
      "prerequisites": {"status": "PASSED", "duration_seconds": 5},
      "redshift_disaster_recovery": {"status": "PASSED", "duration_seconds": 180},
      "neptune_replica_failover": {"status": "PASSED", "duration_seconds": 60},
      "configuration_management": {"status": "PASSED", "duration_seconds": 10},
      "e2e_disaster_recovery": {"status": "PASSED", "duration_seconds": 90}
    },
    "summary": {
      "total_tests": 5,
      "passed": 5,
      "failed": 0,
      "success_rate": "100.0%",
      "overall_status": "PASSED"
    }
  }
}

```text

## 🔍 Test Scenarios

### Redshift Disaster Recovery

1. **Failure Simulation**: Temporarily block access to primary Redshift cluster

2. **Snapshot Retrieval**: Get latest automated snapshot

3. **Restore Process**: Create new cluster from snapshot

4. **Connectivity Test**: Verify restored cluster accessibility

5. **Cleanup**: Optional cleanup of test resources

### Neptune Replica Failover

1. **Replica Discovery**: Identify available Neptune read replicas

2. **Connectivity Testing**: Test both primary and replica endpoints

3. **Read Query Testing**: Verify replica can handle read operations

4. **Performance Comparison**: Compare response times between primary and replica

5. **Configuration Switch**: Update application to use replica endpoint

### End-to-End Scenarios

1. **Configuration Backup**: Save current endpoint configuration

2. **Failover Execution**: Switch to backup/replica endpoints

3. **Functionality Validation**: Test application with backup endpoints

4. **Recovery Process**: Switch back to primary endpoints

5. **Validation**: Confirm normal operations restored

## ⚙️ Configuration

### Environment Variables

```bash

# Required

AWS_REGION="us-east-1"
REDSHIFT_CLUSTER_IDENTIFIER="news-cluster-dev"
NEPTUNE_CLUSTER_IDENTIFIER="neuronews-dev"

# Optional

REDSHIFT_PASSWORD="your-password"  # For connection testing

```text

### Terraform Configuration

Ensure your Terraform configuration includes:

```hcl

# Redshift with automated backups

resource "aws_redshift_cluster" "processed_texts" {
  automated_snapshot_retention_period = 7
  skip_final_snapshot                 = false
}

# Neptune with replica support

variable "neptune_cluster_size" {
  default = 2  # Enables replica creation

}

```text

## 🚨 Safety Considerations

### Production Environment

- **Never run these tests in production** without proper change management

- Use dedicated test/staging environments when possible

- Coordinate with operations teams for production testing windows

### Resource Management

- Test clusters may incur AWS costs

- Scripts include cleanup options but verify resource deletion

- Monitor AWS costs during testing periods

### Rollback Planning

- All scripts include rollback mechanisms

- Original configurations are automatically backed up

- Manual rollback procedures are documented in each script

## 🎯 Success Criteria

The disaster recovery test suite validates:

✅ **Redshift Resilience**

- Automated snapshot creation and retention

- Successful restore from snapshots

- Minimal data loss (RPO < 24 hours)

- Reasonable recovery time (RTO < 2 hours)

✅ **Neptune Availability**

- Replica endpoint accessibility

- Read query performance on replicas

- Seamless failover capability

- Configuration management automation

✅ **Operational Excellence**

- Automated testing and reporting

- Clear documentation and procedures

- Monitoring and alerting integration

- Regular testing schedule capability

## 🔗 Integration

### CI/CD Pipeline Integration

```yaml

# Example GitHub Actions workflow

- name: Run Disaster Recovery Tests

  run: |
    ./scripts/test_disaster_recovery.sh \
      --redshift-cluster "${{ secrets.REDSHIFT_CLUSTER }}" \
      --neptune-cluster "${{ secrets.NEPTUNE_CLUSTER }}" \
      --region "${{ secrets.AWS_REGION }}"

```text

### Monitoring Integration

- Test results can be sent to CloudWatch metrics

- Reports integrate with existing monitoring dashboards

- Alerts can be configured for test failures

## 📚 Additional Resources

- [AWS Redshift Backup and Restore](https://docs.aws.amazon.com/redshift/latest/mgmt/working-with-snapshots.html)

- [AWS Neptune High Availability](https://docs.aws.amazon.com/neptune/latest/userguide/feature-overview-backup-restore.html)

- [Disaster Recovery Best Practices](https://aws.amazon.com/disaster-recovery/)

## 🤝 Contributing

When adding new disaster recovery tests:

1. Follow the existing script structure and logging patterns

2. Include proper error handling and cleanup

3. Update this README with new test descriptions

4. Add appropriate test cases to the main test suite

5. Ensure tests work in both isolated and orchestrated modes

## 📞 Support

For issues with disaster recovery testing:

1. Check test logs in `logs/dr-tests/`

2. Verify AWS permissions and cluster availability

3. Review security group and network configurations

4. Consult the individual script documentation
