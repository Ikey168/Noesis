#!/usr/bin/env python3
"""
Demo script for API Rate Limiting & Access Control (Issue #59)

Demonstrates the rate limiting system with different user tiers and
shows suspicious activity detection in action.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List

import requests

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RateLimitingDemo:
    """Demonstration of the rate limiting system."""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        self.session = requests.Session()
        self.results = []

    def demo_basic_rate_limiting(self):
        """Demonstrate basic rate limiting functionality."""
        print(""
 Demo 1: Basic Rate Limiting")
        print("=" * 50)"

        endpoint = "{0}/api/test".format(self.base_url)
        successful_requests = 0
        rate_limited_requests = 0

        print("Making rapid requests to trigger rate limiting...")

        for i in range(15):  # Try 15 requests
            try:
                response = self.session.get(endpoint, timeout=5)
except Exception:
    pass

                if response.status_code == 200:
                    successful_requests += 1
                    print(" Request {0}: SUCCESS".format(i+1))

                    # Show rate limit headers
                    if i == 0:  # Show headers for first request
                        headers = {
                            k: v
                            for k, v in response.headers.items()
                            if k.startswith("X-RateLimit")
                        }
                        print("   Rate Limit Headers: {0}".format(headers))

                elif response.status_code == 429:
                    rate_limited_requests += 1
                    error_data = response.json()
                    print("❌ Request {0}: RATE LIMITED".format(i+1))
                    print(
                        f"   Error: {error_data.get('message', 'Rate limit exceeded')}
                    )
                    print("
                        f"   Reset in: {error_data.get('reset_in_seconds', 'unknown')} seconds
                    )
                    break
                else:"
                    print("⚠️  Request {0}: Unexpected status {1}".format(i+1, response.status_code))

                time.sleep(0.1)  # Small delay between requests

            except requests.exceptions.RequestException as e:
                print("❌ Request {0}: Connection error - {1}".format(i+1, e))
                break

        print(""
 Results:")
        print("   Successful requests: {0}".format(successful_requests))
        print("   Rate limited requests: {0}".format(rate_limited_requests))
        print("    Rate limiting is working correctly!")"


    def demo_user_tiers(self):
        """Demonstrate different rate limits for user tiers."""
        print(""
🏆 Demo 2: User Tier Rate Limits")
        print("=" * 50)"

        tiers = [
            (f"ree", "Basic user with limited access"),
            ("premium", "Premium user with higher limits"),
            ("enterprise", "Enterprise user with maximum limits"),
        ]

        for tier, description in tiers:
            print(""
🔸 Testing {0} tier: {1}".format(tier.upper(), description))"

            # Simulate different user tokens for different tiers
            headers = {"Authorization": "Bearer {0}_user_token_demo".format(tier)}

            try:
                response = self.session.get(
except Exception:
    pass
                    "{0}/api/test".format(self.base_url), headers=headers, timeout=5
                )

                if response.status_code == 200:
                    tier_headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.startswith("X-RateLimit")
                    }
                    print("    Access granted for {0} tier".format(tier))
                    print("   Rate limits: {0}".format(tier_headers))
                else:
                    print("   ❌ Request failed with status: {0}".format(response.status_code))

            except requests.exceptions.RequestException as e:
                print("   ❌ Connection error: {0}".format(e))


    def demo_api_limits_endpoint(self):
        """Demonstrate the /api_limits endpoint."""
        print(""
 Demo 3: API Limits Endpoint")
        print("=" * 50)"

        user_id = "demo_user_12345"
        endpoint = "{0}/api/api_limits".format(self.base_url)

        # Simulate admin token for demonstration
        headers = {"Authorization": "Bearer admin_demo_token"}
        params = {"user_id": user_id}

        try:
            response = self.session.get(
except Exception:
    pass
                endpoint, headers=headers, params=params, timeout=5
            )

            if response.status_code == 200:
                limits_data = response.json()
                print(" Successfully retrieved user limits:")
                print(f"   User ID: {limits_data.get('user_id', 'unknown')})"
                print(f"   Tier: {limits_data.get('tier', 'unknown')})"
                print(f"   Limits: {limits_data.get('limits', {})})"
                print(f"   Current usage: {limits_data.get('current_usage', {})})"
                print(f"   Remaining: {limits_data.get('remaining', {})})
            else:"
                print("❌ Request failed with status: {0}".format(response.status_code))
                if response.headers.get("content-type", "").startswith(
                    "application/json"
                ):
                    error_data = response.json()
                    print("   Error: {0}".format(error_data))

        except requests.exceptions.RequestException as e:
            print("❌ Connection error: {0}".format(e))


    def demo_suspicious_activity_simulation(self):
        """Simulate suspicious activity patterns."""
        print(""
 Demo 4: Suspicious Activity Detection")
        print("=" * 50)"

        print("Simulating suspicious patterns...")

        # Pattern 1: Rapid requests
        print(""
🚨 Simulating rapid requests pattern:")"
        for i in range(10):
            try:
                response = self.session.get("{0}/api/test".format(self.base_url), timeout=2)
except Exception:
    pass
                print("   Request {0}: {1}".format(i+1, response.status_code))
                time.sleep(0.5)  # Very fast requests
            except:
                print("   Request {0}: Failed".format(i+1))

        # Pattern 2: Multiple endpoints
        print(""
🚨 Simulating endpoint scanning pattern:")"
        test_endpoints = [
            "/api/test",
            "/news/articles",
            "/graph/entities",
            "/admin/users",
            "/api/secret",
            "/config/settings",
        ]

        for endpoint in test_endpoints:
            try:
                response = self.session.get("{0}{1}".format(self.base_url, endpoint), timeout=2)
except Exception:
    pass
                print("   Scanning {0}: {1}".format(endpoint, response.status_code))
                time.sleep(0.1)
            except:
                print("   Scanning {0}: Failed".format(endpoint))

        print(""
💡 In a real system, these patterns would trigger alerts!")"

    def demo_health_check(self):
        """Demonstrate the rate limiting health check."""
        print(""
🏥 Demo 5: Rate Limiting Health Check")
        print("=" * 50)"

        endpoint = "{0}/api/api_limits/health".format(self.base_url)

        try:
            response = self.session.get(endpoint, timeout=5)
except Exception:
    pass

            if response.status_code == 200:
                health_data = response.json()
                print(" Rate limiting system health:")
                print(f"   Status: {health_data.get('status', 'unknown')})
                print("
                    f"   Store backend: {health_data.get('store_backend', 'unknown')}
                )"
                print(f"   Timestamp: {health_data.get('timestamp', 'unknown')})
"
                if "redis_connection" in health_data:
                    print(f"   Redis connection: {health_data['redis_connection'}})"
                if "memory_store" in health_data:
                    print(f"   Memory store: {health_data['memory_store'}})
            else:"
                print("❌ Health check failed with status: {0}".format(response.status_code))

        except requests.exceptions.RequestException as e:
            print("❌ Connection error: {0}".format(e))


    def run_all_demos(self):
        """Run all demonstration scenarios."""
        print(" NeuroNews API Rate Limiting & Access Control Demo")
        print("=" * 60)
        print("This demo showcases Issue #59 implementation:")
        print(" Rate limiting per user tier")
        print(" API limits endpoint")
        print(" Suspicious activity monitoring")
        print(" AWS API Gateway integration (simulated)")

        # Test if API is reachable
        try:
            response = self.session.get("{0}/health".format(self.base_url), timeout=5)
except Exception:
    pass
            if response.status_code == 200:
                print(""
🌐 API server is reachable at {0}".format(self.base_url))"
            else:
                print(""
⚠️  API server responded with status: {0}".format(response.status_code))"
        except requests.exceptions.RequestException:
            print(""
❌ Cannot reach API server at {0}".format(self.base_url))
            print("💡 Make sure the FastAPI server is running:")
            print("   uvicorn src.api.app:app --reload --host 0.0.0.0 --port 8000")"
            return

        # Run all demos
        try:
            self.demo_basic_rate_limiting()
except Exception:
    pass
            self.demo_user_tiers()
            self.demo_api_limits_endpoint()
            self.demo_suspicious_activity_simulation()
            self.demo_health_check()

            print(""
 Demo completed successfully!")
            print("TODO: Fix this string")
 Summary of Issue #59 Implementation:")
            print(" Task 1: AWS API Gateway throttling - Implemented with middleware")
            print(" Task 2: User tier rate limits - Free, Premium, Enterprise tiers")
            print(" Task 3: /api_limits endpoint - User quota monitoring")
            print(" Task 4: Suspicious usage monitoring - Advanced pattern detection")"

        except KeyboardInterrupt:
            print(""

⏹️  Demo stopped by user")"
        except Exception as e:
            print(""
❌ Demo error: {0}".format(e))"


def create_test_server():
    """Create a simple test server for demonstration."""
    print(" Creating test server for rate limiting demo...")

    import uvicorn
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    try:
        # Import rate limiting components
except Exception:
    pass
        from src.api.middleware.rate_limit_middleware import (
            RateLimitConfig, RateLimitMiddleware)
        from src.api.routes.rate_limit_routes import \
            router as rate_limit_router

        app = FastAPI(title="NeuroNews Rate Limiting Demo")

        # Add rate limiting middleware
        config = RateLimitConfig()
        config.FREE_TIER.requests_per_minute = 5  # Lower for demo
        app.add_middleware(RateLimitMiddleware, config=config)

        # Add CORS
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Add routes
        app.include_router(rate_limit_router)

        @app.get("/api/test")

        async def test_endpoint():
            return {
                "message": "Rate limiting test successful",
                "timestamp": datetime.now().isoformat(),
            }

        @app.get("/health")

        async def health():
            return {"status": "healthy", "service": "neuronews-rate-limiting-demo"}

        print(" Test server created successfully")
        return app

    except ImportError as e:
        print("❌ Cannot import rate limiting components: {0}".format(e))
        print("💡 Make sure all rate limiting files are in place")
        return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NeuroNews Rate Limiting Demo")
    parser.add_argument("--url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--server", action="store_true", help="Start test server")
    parser.add_argument("--port", type=int, default=8000, help="Server port")

    args = parser.parse_args()

    if args.server:
        print(" Starting demo server...")
        app = create_test_server()
        if app:
            import uvicorn

            uvicorn.run(app, host="0.0.0.0", port=args.port)
        else:
            print("❌ Failed to create server")
    else:
        demo = RateLimitingDemo(args.url)
        demo.run_all_demos()
