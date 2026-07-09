"""
Iceberg table compaction job
Issue #293

Performs compaction operations on Iceberg tables:
1. rewrite_data_files - consolidates small files into larger ones
2. rewrite_manifests - consolidates manifest files for better query performance

Usage:
python iceberg_compaction.py --table demo.news.articles_enriched --operation rewrite_data_files
python iceberg_compaction.py --table demo.news.articles_enriched --operation rewrite_manifests
"""
import argparse
import sys
from pyspark.sql import SparkSession
from datetime import datetime

def create_spark_session():
    """Create Spark session with Iceberg support."""
    return SparkSession.builder \
        .appName("IcebergCompaction") \
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

def get_table_metrics_before(spark, table_name):
    """Get table metrics before compaction."""
    try:
        # Get file count and size info
        files_df = spark.sql(f"SELECT * FROM {table_name}.files")
        file_count = files_df.count()
        
        # Get snapshot info
        snapshots_df = spark.sql(f"SELECT * FROM {table_name}.snapshots")
        snapshot_count = snapshots_df.count()
        
        print(f"📊 Before compaction metrics for {table_name}:")
        print(f"   - Files: {file_count}")
        print(f"   - Snapshots: {snapshot_count}")
        
        return {
            'file_count': file_count,
            'snapshot_count': snapshot_count
        }
    except Exception as e:
        print(f"⚠️  Could not get before metrics: {e}")
        return {'file_count': 0, 'snapshot_count': 0}

def get_table_metrics_after(spark, table_name, before_metrics):
    """Get table metrics after compaction and show improvement."""
    try:
        files_df = spark.sql(f"SELECT * FROM {table_name}.files")
        file_count = files_df.count()
        
        snapshots_df = spark.sql(f"SELECT * FROM {table_name}.snapshots")
        snapshot_count = snapshots_df.count()
        
        file_reduction = before_metrics['file_count'] - file_count
        
        print(f"📊 After compaction metrics for {table_name}:")
        print(f"   - Files: {file_count} (reduced by {file_reduction})")
        print(f"   - Snapshots: {snapshot_count}")
        
        return {
            'file_count': file_count,
            'snapshot_count': snapshot_count,
            'file_reduction': file_reduction
        }
    except Exception as e:
        print(f"⚠️  Could not get after metrics: {e}")
        return {'file_count': 0, 'snapshot_count': 0, 'file_reduction': 0}

def rewrite_data_files(spark, table_name):
    """Rewrite data files to consolidate small files."""
    print(f"🔄 Starting data files rewrite for {table_name}...")
    
    # Extract catalog from table name
    catalog = table_name.split('.')[0]
    
    # Call Iceberg procedure for data file rewrite
    result = spark.sql(f"CALL {catalog}.system.rewrite_data_files(table => '{table_name}')")
    
    print("✅ Data files rewrite completed")
    result.show()
    return result

def rewrite_manifests(spark, table_name):
    """Rewrite manifest files to improve query performance."""
    print(f"🔄 Starting manifest rewrite for {table_name}...")
    
    # Extract catalog from table name
    catalog = table_name.split('.')[0]
    
    # Call Iceberg procedure for manifest rewrite
    result = spark.sql(f"CALL {catalog}.system.rewrite_manifests(table => '{table_name}')")
    
    print("✅ Manifest rewrite completed")
    result.show()
    return result

def main():
    parser = argparse.ArgumentParser(description='Iceberg table compaction')
    parser.add_argument('--table', required=True, help='Table name (e.g., demo.news.articles_enriched)')
    parser.add_argument('--operation', required=True, choices=['rewrite_data_files', 'rewrite_manifests'],
                       help='Compaction operation to perform')
    
    args = parser.parse_args()
    
    spark = None
    try:
        spark = create_spark_session()
        
        # Verify table exists
        if not spark.catalog.tableExists(args.table):
            print(f"❌ Table {args.table} does not exist")
            return 1
        
        # Get before metrics
        before_metrics = get_table_metrics_before(spark, args.table)
        
        # Perform the requested operation
        if args.operation == 'rewrite_data_files':
            rewrite_data_files(spark, args.table)
        elif args.operation == 'rewrite_manifests':
            rewrite_manifests(spark, args.table)
        
        # Get after metrics and show improvement
        after_metrics = get_table_metrics_after(spark, args.table, before_metrics)
        
        # DoD: Show reduced small-file count
        if after_metrics['file_reduction'] > 0:
            print(f"✅ DoD satisfied: Reduced file count by {after_metrics['file_reduction']} files")
        else:
            print("ℹ️  No file reduction (table may already be optimized)")
        
        print(f"✅ Compaction operation '{args.operation}' completed successfully")
        return 0
        
    except Exception as e:
        print(f"❌ Error during compaction: {e}")
        return 1
    finally:
        if spark:
            spark.stop()

if __name__ == "__main__":
    sys.exit(main())
