# Anti-Detection System Documentation

## Overview

The NeuroNews anti-detection system provides comprehensive protection against IP bans, CAPTCHA challenges, and bot detection when scraping news websites. It includes proxy rotation, user agent randomization, CAPTCHA solving, and Tor integration.

## Components

### 1. Proxy Rotation Manager (`proxy_manager.py`)

The `ProxyRotationManager` handles automatic proxy rotation with health monitoring and load balancing.

#### Features:

- **Multiple rotation strategies**: round-robin, random, health-based

- **Health monitoring**: Automatic proxy health checks and scoring

- **Connection limiting**: Prevents overloading individual proxies

- **Failure handling**: Automatic retry and proxy blocking

- **Statistics tracking**: Success rates, response times, usage patterns

#### Configuration:

```json

{
  "proxy_settings": {
    "rotation_strategy": "health_based",
    "health_check_interval": 300,
    "retry_failed_proxies": true
  },
  "proxies": [
    {
      "host": "proxy1.example.com",
      "port": 8080,
      "proxy_type": "http",
      "username": "user",
      "password": "pass",
      "concurrent_limit": 5,
      "location": "US",
      "provider": "ProxyProvider"
    }
  ]
}

```text

#### Usage:

```python

from scraper.proxy_manager import ProxyRotationManager

# Initialize with config file

manager = ProxyRotationManager(config_path="config_proxy.json")

# Get next proxy

proxy = await manager.get_proxy()

# Record request result for health tracking

await manager.record_request(proxy, success=True, response_time=0.5)

# Check proxy statistics

stats = manager.get_proxy_stats()

```text

### 2. User Agent Rotator (`user_agent_rotator.py`)

The `UserAgentRotator` provides realistic browser profiles and automatic header rotation.

#### Features:

- **Realistic user agents**: Chrome and Firefox profiles

- **Automatic rotation**: Per-request or time-based rotation

- **Complete headers**: Accept, Accept-Language, Accept-Encoding, etc.

- **Mobile support**: Optional mobile user agents

- **Session management**: Maintains consistency per session

#### Usage:

```python

from scraper.user_agent_rotator import UserAgentRotator

rotator = UserAgentRotator()

# Get random user agent

user_agent = rotator.get_user_agent()

# Get complete headers

headers = rotator.get_random_headers()

# Force rotation to new profile

rotator.rotate_profile()

```text

### 3. CAPTCHA Solver (`captcha_solver.py`)

The `CaptchaSolver` automatically detects and solves CAPTCHAs using the 2Captcha service.

#### Features:

- **Automatic detection**: Identifies reCAPTCHA v2 and hCaptcha

- **Multiple CAPTCHA types**: Support for image and audio CAPTCHAs

- **Async solving**: Non-blocking CAPTCHA resolution

- **Retry logic**: Automatic retries on failure

- **Timeout handling**: Configurable solving timeouts

#### Configuration:

```python

# Set your 2Captcha API key

captcha_solver = CaptchaSolver(api_key="your_2captcha_api_key")

```text

#### Usage:

```python

from scraper.captcha_solver import CaptchaSolver

solver = CaptchaSolver(api_key="your_api_key")

# Detect CAPTCHA in page content

if await solver.detect_captcha(page_html):
    # Solve reCAPTCHA v2

    token = await solver.solve_recaptcha_v2(
        site_key="site_key_from_page",
        page_url="https://example.com"
    )

    # Use token in form submission

    form_data["g-recaptcha-response"] = token

```text

### 4. Tor Manager (`tor_manager.py`)

The `TorManager` provides Tor proxy integration and identity rotation.

#### Features:

- **Tor proxy support**: SOCKS5 proxy through Tor network

- **Identity rotation**: Change Tor circuit for new IP

- **Control port access**: Communicate with Tor control port

- **Automatic setup**: Easy Tor proxy configuration

#### Prerequisites:

```bash

# Install and configure Tor

sudo apt-get install tor
sudo systemctl start tor

# Configure control port in /etc/tor/torrc:

# ControlPort 9051

# HashedControlPassword your_hashed_password

```text

#### Usage:

```python

from scraper.tor_manager import TorManager

tor_manager = TorManager(
    control_port=9051,
    control_password="your_password"
)

# Get Tor proxy URL

proxy_url = tor_manager.get_proxy_url()

# Rotate identity for new IP

await tor_manager.rotate_identity()

```text

## Integration with AsyncNewsScraperEngine

The anti-detection system integrates seamlessly with the main scraper engine:

```python

from scraper.async_scraper_engine import AsyncNewsScraperEngine

# Initialize with anti-detection features

scraper = AsyncNewsScraperEngine(
    proxy_config_path="config_proxy.json",
    use_tor=True,
    captcha_api_key="your_2captcha_key",
    user_agent_rotation=True
)

# Scraping automatically uses anti-detection

await scraper.start()
results = await scraper.scrape_url("https://example.com/news")

```text

## Configuration Guide

### 1. Basic Setup

Create `config_proxy.json` with your proxy configuration:

```json

{
  "proxy_settings": {
    "rotation_strategy": "health_based",
    "health_check_interval": 300,
    "retry_failed_proxies": true,
    "max_retries_per_proxy": 3,
    "request_delay_range": [1, 3]
  },
  "proxies": [
    {
      "host": "your-proxy.com",
      "port": 8080,
      "proxy_type": "http",
      "username": "username",
      "password": "password",
      "concurrent_limit": 5
    }
  ],
  "captcha_settings": {
    "enabled": true,
    "api_key": "YOUR_2CAPTCHA_API_KEY",
    "timeout": 120
  }
}

```text

### 2. Proxy Providers

Recommended proxy providers:

- **Bright Data** (formerly Luminati): High-quality residential proxies

- **Oxylabs**: Data center and residential proxies

- **Smartproxy**: Affordable residential proxies

- **ProxyMesh**: Rotating HTTP proxies

- **Storm Proxies**: Rotating residential proxies

### 3. 2Captcha Setup

1. Sign up at [2captcha.com](https://2captcha.com)

2. Get your API key from the dashboard

3. Add API key to configuration

4. Fund your account for CAPTCHA solving

### 4. Tor Setup

```bash

# Ubuntu/Debian

sudo apt-get install tor

# macOS

brew install tor

# Configure Tor (edit /etc/tor/torrc or /usr/local/etc/tor/torrc)

ControlPort 9051
HashedControlPassword $(tor --hash-password "your_password")

# Start Tor

sudo systemctl start tor  # Linux

brew services start tor   # macOS

```text

## Best Practices

### 1. Proxy Management

- Use high-quality residential proxies when possible

- Implement appropriate delays between requests (1-3 seconds)

- Monitor proxy health and remove failing proxies

- Rotate proxies regularly to avoid detection

- Use geographically distributed proxies

### 2. User Agent Rotation

- Enable automatic rotation for every request or session

- Use realistic browser profiles (Chrome/Firefox)

- Include complete header sets, not just User-Agent

- Avoid outdated or suspicious user agent strings

- Match headers to the chosen user agent

### 3. CAPTCHA Handling

- Monitor CAPTCHA detection rates

- Implement delays after CAPTCHA solving

- Use high-quality CAPTCHA solving services

- Have fallback strategies for unsolvable CAPTCHAs

- Consider human review for complex CAPTCHAs

### 4. Rate Limiting

- Implement exponential backoff on failures

- Respect robots.txt when possible

- Use random delays between requests

- Monitor response codes and adjust accordingly

- Implement circuit breakers for problematic sites

### 5. Detection Avoidance

- Vary request patterns and timing

- Use realistic browsing behavior simulation

- Implement session management

- Avoid suspicious request signatures

- Monitor for detection indicators

## Troubleshooting

### Common Issues

#### Proxy Connection Failures

```python

# Check proxy health

healthy = await proxy_manager.check_proxy_health(proxy)
if not healthy:
    # Remove or replace proxy

    await proxy_manager.remove_unhealthy_proxies()

```text

#### CAPTCHA Solving Failures

```python

# Increase timeout for complex CAPTCHAs

solver = CaptchaSolver(api_key="key", timeout=180)

# Check 2Captcha account balance

balance = await solver.get_balance()
if balance < 1.0:
    logger.warning("Low 2Captcha balance")

```text

#### Tor Connection Issues

```python

# Test Tor connectivity

import aiohttp
async with aiohttp.ClientSession() as session:
    proxy_url = tor_manager.get_proxy_url()
    async with session.get(
        "http://httpbin.org/ip",
        proxy=proxy_url
    ) as response:
        data = await response.json()
        print(f"Tor IP: {data['origin']}")

```text

### Logging and Monitoring

Enable detailed logging for debugging:

```python

import logging

# Configure logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'

)

# Monitor proxy statistics

stats = proxy_manager.get_proxy_stats()
for proxy_key, proxy_stats in stats.items():
    print(f"{proxy_key}: Success rate {proxy_stats['success_rate']:.2%}")

```text

## Performance Optimization

### 1. Concurrent Connections

- Use appropriate concurrent limits per proxy

- Implement connection pooling

- Monitor resource usage

### 2. Caching

- Cache successful proxy health checks

- Store working user agent profiles

- Cache CAPTCHA solving results when applicable

### 3. Resource Management

- Close unused connections promptly

- Implement proper cleanup on shutdown

- Monitor memory usage with large proxy pools

## Security Considerations

### 1. Credential Protection

- Store API keys securely (environment variables)

- Use encrypted configuration files

- Implement credential rotation

### 2. Traffic Analysis

- Use HTTPS whenever possible

- Implement request signing when required

- Monitor for traffic pattern analysis

### 3. Legal Compliance

- Respect terms of service

- Implement rate limiting

- Follow applicable laws and regulations

- Consider ethical scraping practices

## Example Implementation

Complete example integrating all anti-detection features:

```python

import asyncio
from scraper.async_scraper_engine import AsyncNewsScraperEngine

async def main():
    # Initialize scraper with full anti-detection

    scraper = AsyncNewsScraperEngine(
        proxy_config_path="config_proxy.json",
        use_tor=False,  # Use proxy rotation instead

        captcha_api_key="your_2captcha_key",
        user_agent_rotation=True,
        max_concurrent=5,
        request_delay=(1, 3)
    )

    try:
        await scraper.start()

        # List of news sites to scrape

        urls = [
            "https://news.ycombinator.com",
            "https://techcrunch.com",
            "https://arstechnica.com"
        ]

        # Scrape with anti-detection

        for url in urls:
            try:
                result = await scraper.scrape_url(url)
                print(f"Scraped {len(result.get('articles', []))} articles from {url}")

                # Add delay between sites

                await asyncio.sleep(2)

            except Exception as e:
                print(f"Error scraping {url}: {e}")

        # Get proxy statistics

        stats = scraper.proxy_manager.get_proxy_stats()
        print(f"Proxy stats: {stats}")

    finally:
        await scraper.stop()

if __name__ == "__main__":
    asyncio.run(main())

```text

This comprehensive anti-detection system provides robust protection against common scraping challenges while maintaining high performance and reliability.
