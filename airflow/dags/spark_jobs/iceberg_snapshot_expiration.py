"""
Iceberg snapshot expiration job
Issue #293

Expires old snapshots from Iceberg tables to manage storage and history.
Retains a configurable number of recent snapshots while removing older ones.

Usage:
python iceberg_snapshot_expiration.py --table demo.news.articles_enriched --older_than "2024-01-01 00:00:00" --retain_last 5
"""
import argparse
import sys
from pyspark.sql import SparkSession
from datetime import datetime

def create_spark_session():
    """Create Spark session with Iceberg support."""
    return SparkSession.builder \
        .appName("IcebergSnapshotExpiration") \
        .config("spark.sql.catalog.demo", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.demo.catalog-impl", "org.apache.iceberg.rest.RESTCatalog") \
        .config("spark.sql.catalog.demo.uri", "http://localhost:8181") \
        .config("spark.sql.catalog.demo.warehouse", "s3a://demo-warehouse/") \
        .config("spark.sql.catalog.demo.io-impl", "org.apache.iceberg.aws.s3.S3FileIO") \
        .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.local.type", "hadoop") \
        .config("spark.sql.catalog.local.warehouse", "/warehouse") \
        .config("spark.hadoop.fs.s3a.access.key", "minioadmin") \
        .config("spark.hadoop.fs.s3a.secret.key", "minioadmin") \
        .config("spark.hadoop.fs.s3a.endpoint", "http://localhost:9000") \
        .config("spark.hadoop.fs.s3a.path.style.access", "true") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .getOrCreate()

def get_snapshot_metrics_before(spark, table_name):
    """Get snapshot metrics before expiration."""
    try:
        snapshots_df = spark.sql(f"SELECT * FROM {table_name}.snapshots ORDER BY committed_at DESC")
        snapshot_count = snapshots_df.count()
        
        print(f"📊 Before expiration metrics for {table_name}:")
        print(f"   - Total snapshots: {snapshot_count}")
        
        # Show recent snapshots
        if snapshot_count > 0:
            print("   - Recent snapshots:")
            snapshots_df.select("snapshot_id", "committed_at").show(10, truncate=False)
        
        return {'snapshot_count': snapshot_count}
    except Exception as e:
        print(f"⚠️  Could not get before metrics: {e}")
        return {'snapshot_count': 0}

def get_snapshot_metrics_after(spark, table_name, before_metrics):
    """Get snapshot metrics after expiration and show improvement."""
    try:
        snapshots_df = spark.sql(f"SELECT * FROM {table_name}.snapshots ORDER BY committed_at DESC")
        snapshot_count = snapshots_df.count()
        
        snapshots_removed = before_metrics['snapshot_count'] - snapshot_count
        
        print(f"📊 After expiration metrics for {table_name}:")
        print(f"   - Total snapshots: {snapshot_count} (removed {snapshots_removed})")
        
        # Show remaining snapshots
        if snapshot_count > 0:
            print("   - Remaining snapshots:")
            snapshots_df.select("snapshot_id", "committed_at").show(truncate=False)
        
        return {
            'snapshot_count': snapshot_count,
            'snapshots_removed': snapshots_removed
        }
    except Exception as e:
        print(f"⚠️  Could not get after metrics: {e}")
        return {'snapshot_count': 0, 'snapshots_removed': 0}

def expire_snapshots(spark, table_name, older_than, retain_last):
    """Expire old snapshots while retaining recent ones."""
    print(f"🔄 Starting snapshot expiration for {table_name}...")
    print(f"   - Expiring snapshots older than: {older_than}")
    print(f"   - Retaining last: {retain_last} snapshots")
    
    # Extract catalog from table name
    catalog = table_name.split('.')[0]
    
    # Call Iceberg procedure for snapshot expiration
    expire_sql = f"""
    CALL {catalog}.system.expire_snapshots(
        table => '{table_name}',
        older_than => TIMESTAMP '{older_than}',
        retain_last => {retain_last}
    )
    """
    
    result = spark.sql(expire_sql)
    
    print("✅ Snapshot expiration completed")
    result.show()
    return result

def main():
    parser = argparse.ArgumentParser(description='Iceberg snapshot expiration')
    parser.add_argument('--table', required=True, help='Table name (e.g., demo.news.articles_enriched)')
    parser.add_argument('--older_than', required=True, help='Expire snapshots older than this timestamp (YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--retain_last', type=int, default=5, help='Number of recent snapshots to retain (default: 5)')
    
    args = parser.parse_args()
    
    spark = None
    try:
        spark = create_spark_session()
        
        # Verify table exists
        if not spark.catalog.tableExists(args.table):
            print(f"❌ Table {args.table} does not exist")
            return 1
        
        # Get before metrics
        before_metrics = get_snapshot_metrics_before(spark, args.table)
        
        # Perform snapshot expiration
        expire_snapshots(spark, args.table, args.older_than, args.retain_last)
        
        # Get after metrics and show improvement
        after_metrics = get_snapshot_metrics_after(spark, args.table, before_metrics)
        
        # DoD: Show trimmed history
        if after_metrics['snapshots_removed'] > 0:
            print(f"✅ DoD satisfied: Trimmed history by removing {after_metrics['snapshots_removed']} snapshots")
        else:
            print("ℹ️  No snapshots removed (all snapshots within retention policy)")
        
        print(f"✅ Snapshot expiration completed successfully")
        return 0
        
    except Exception as e:
        print(f"❌ Error during snapshot expiration: {e}")
        return 1
    finally:
        if spark:
            spark.stop()

if __name__ == "__main__":
    sys.exit(main())
