"""
In-memory sample data for examples/semantic/catalog.yaml — five fake
security sources registered in DuckAPI exactly like real connectors
would be (functions returning list[dict], with equality push-down params).
"""

from datetime import datetime, timedelta
from typing import Optional


def build_sources(now: datetime) -> dict:
    def ago(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat()

    proxy = [
        {"timestamp": ago(2), "username": "alice", "source_ip": "10.0.0.11", "destination_domain": "github.com", "action": "ALLOW"},
        {"timestamp": ago(5), "username": "bob", "source_ip": "10.0.0.12", "destination_domain": "api.github.com", "action": "ALLOW"},
        {"timestamp": ago(30), "username": "carol", "source_ip": "10.0.0.13", "destination_domain": "github.com", "action": "ALLOW"},
        {"timestamp": ago(1), "username": "dave", "source_ip": "10.0.0.14", "destination_domain": "news.example.org", "action": "BLOCK"},
    ]
    dns = [
        {"timestamp": ago(3), "client_ip": "10.0.0.11", "query_name": "example.com", "response_code": "NOERROR"},
        {"timestamp": ago(4), "client_ip": "10.0.0.14", "query_name": "www.example.com", "response_code": "NOERROR"},
        {"timestamp": ago(6), "client_ip": "10.0.0.12", "query_name": "github.com", "response_code": "NOERROR"},
    ]
    firewall = [
        {"timestamp": ago(1), "src_ip": "10.0.0.11", "dst_ip": "10.0.0.5", "dst_port": 443, "action": "ALLOW"},
        {"timestamp": ago(2), "src_ip": "10.0.0.12", "dst_ip": "10.0.0.5", "dst_port": 22, "action": "DENY"},
        {"timestamp": ago(3), "src_ip": "10.0.0.14", "dst_ip": "203.0.113.9", "dst_port": 443, "action": "ALLOW"},
        {"timestamp": ago(4), "src_ip": "10.0.0.13", "dst_ip": "203.0.113.9", "dst_port": 3389, "action": "DENY"},
        {"timestamp": ago(5), "src_ip": "10.0.0.11", "dst_ip": "198.51.100.7", "dst_port": 25, "action": "DENY"},
    ]
    auth = [
        {"timestamp": ago(1), "username": "alice", "source_ip": "10.0.0.11", "outcome": "success"},
        {"timestamp": ago(2), "username": "bob", "source_ip": "10.0.0.12", "outcome": "failure"},
        {"timestamp": ago(3), "username": "erin", "source_ip": "10.0.0.99", "outcome": "failure"},
    ]
    assets = [
        {"ip_address": "10.0.0.11", "hostname": "ws-alice", "owner": "alice", "environment": "production"},
        {"ip_address": "10.0.0.12", "hostname": "ws-bob", "owner": "bob", "environment": "staging"},
        {"ip_address": "10.0.0.13", "hostname": "srv-build", "owner": "carol", "environment": "production"},
        {"ip_address": "10.0.0.14", "hostname": "ws-dave", "owner": "dave", "environment": "development"},
        {"ip_address": "10.0.0.5", "hostname": "srv-git", "owner": "platform", "environment": "production"},
    ]

    def proxy_logs(username: Optional[str] = None, action: Optional[str] = None, limit: Optional[int] = None):
        """Web proxy requests (username/action push down)."""
        rows = [r for r in proxy if (username is None or r["username"] == username) and (action is None or r["action"] == action)]
        return rows[:limit] if limit else rows

    def dns_logs(limit: Optional[int] = None):
        """DNS resolver lookups."""
        return dns[:limit] if limit else dns

    def firewall_logs(action: Optional[str] = None, limit: Optional[int] = None):
        """Firewall connections (action pushes down)."""
        rows = [r for r in firewall if action is None or r["action"] == action]
        return rows[:limit] if limit else rows

    def auth_logs(outcome: Optional[str] = None, limit: Optional[int] = None):
        """Sign-in attempts (outcome pushes down)."""
        rows = [r for r in auth if outcome is None or r["outcome"] == outcome]
        return rows[:limit] if limit else rows

    def asset_inventory(environment: Optional[str] = None, limit: Optional[int] = None):
        """CMDB assets (environment pushes down)."""
        rows = [r for r in assets if environment is None or r["environment"] == environment]
        return rows[:limit] if limit else rows

    return {
        "proxy_logs": proxy_logs,
        "dns_logs": dns_logs,
        "firewall_logs": firewall_logs,
        "auth_logs": auth_logs,
        "asset_inventory": asset_inventory,
    }


def tables(now: Optional[str] = None) -> dict:
    """Entry point for the `python` connector (see duckduck.local.json)."""
    moment = datetime.fromisoformat(now) if now else datetime.utcnow().replace(microsecond=0)
    return build_sources(moment)


def register_sample_sources(duck, now: datetime) -> None:
    for name, fn in build_sources(now).items():
        duck.register_api_function(name, fn)
