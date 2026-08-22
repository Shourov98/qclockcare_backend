"""Dev-only fake GPS pinger.

Simulates a staff mobile app that:
  1. Logs in (email + password) to obtain an access token.
  2. Calls POST /visits/{visit_id}/start-location-sharing with a seed
     position.
  3. Posts /visits/{visit_id}/location-ping every 15 seconds with a
     jittered walk around the seed point.
  4. Posts /visits/{visit_id}/stop-location-sharing on Ctrl-C.

Intended use: open the EVV Live Monitor in the admin dashboard, then
run this script against an active visit to populate the map without
needing the staff app. Admin cookies / CSRF aren't required because
the script uses Bearer auth.

Run:

    uv run python scripts/fake_location_pinger.py \\
        --base-url https://qclockcare-backend.onrender.com \\
        --email staff@qlockcare.dev \\
        --password StaffDevPass123! \\
        --visit-id <UUID> \\
        --base-lat 44.9778 --base-lng -93.2650

Flags:
  --interval        Seconds between pings (default 15)
  --duration        Stop after N seconds (default: run until Ctrl-C)
  --radius          Random walk radius in degrees (default 0.0008, ~80m)
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import time
import uuid
from typing import Any

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake a staff mobile GPS pinger against a live visit.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", required=True, help="API root URL")
    parser.add_argument("--email", required=True, help="Staff login email")
    parser.add_argument("--password", required=True, help="Staff password")
    parser.add_argument("--visit-id", required=True, help="Visit UUID")
    parser.add_argument("--base-lat", type=float, required=True)
    parser.add_argument("--base-lng", type=float, required=True)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--radius", type=float, default=0.0008)
    parser.add_argument(
        "--device-id",
        default="fake-staff-pinger/dev",
        help="device_id sent on each ping",
    )
    return parser.parse_args()


def _walk(base_lat: float, base_lng: float, radius: float) -> tuple[float, float]:
    """Return a jittered (lat, lng) around the base point."""
    # Convert radius from degrees to a rough step using uniform random
    # direction in a small circle. Keeps the marker visibly moving on
    # the EVV map without ever drifting far from the seed location.
    theta = random.uniform(0, 2 * math.pi)
    r = math.sqrt(random.random()) * radius
    delta_lat = r * math.cos(theta)
    delta_lng = r * math.sin(theta) / max(math.cos(math.radians(base_lat)), 1e-6)
    return base_lat + delta_lat, base_lng + delta_lng


async def _login(client: httpx.AsyncClient, base_url: str, email: str, password: str) -> str:
    resp = await client.post(
        f"{base_url}/auth/login",
        json={"email": email, "password": password},
    )
    resp.raise_for_status()
    body = resp.json()
    token: str = body["access_token"]
    print(f"[pinger] logged in as {email}", flush=True)
    return token


async def _start_sharing(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    visit_id: str,
    lat: float,
    lng: float,
) -> None:
    resp = await client.post(
        f"{base_url}/visits/{visit_id}/start-location-sharing",
        headers=headers,
        json={"initial_lat": lat, "initial_lng": lng},
    )
    resp.raise_for_status()
    print(f"[pinger] started sharing at ({lat:.6f}, {lng:.6f})", flush=True)


async def _ping(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    visit_id: str,
    lat: float,
    lng: float,
    device_id: str,
) -> None:
    body: dict[str, Any] = {
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "accuracy_m": 12.5,
        "device_id": device_id,
    }
    resp = await client.post(
        f"{base_url}/visits/{visit_id}/location-ping",
        headers=headers,
        json=body,
    )
    resp.raise_for_status()
    print(f"[pinger] ping at ({lat:.6f}, {lng:.6f})", flush=True)


async def _stop_sharing(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    visit_id: str,
) -> None:
    resp = await client.post(
        f"{base_url}/visits/{visit_id}/stop-location-sharing",
        headers=headers,
    )
    if resp.status_code >= 400:
        # Best-effort: the visit may already be over.
        print(
            f"[pinger] stop-location-sharing returned {resp.status_code}: {resp.text}",
            file=sys.stderr,
            flush=True,
        )
        return
    print("[pinger] stopped sharing", flush=True)


async def run(args: argparse.Namespace) -> None:
    try:
        uuid.UUID(args.visit_id)
    except ValueError:
        print("[pinger] --visit-id must be a UUID", file=sys.stderr)
        sys.exit(2)

    base_url = args.base_url.rstrip("/")
    timeout = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await _login(client, base_url, args.email, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        # Seed position
        seed_lat, seed_lng = _walk(args.base_lat, args.base_lng, 0)
        await _start_sharing(
            client, base_url, headers, args.visit_id, seed_lat, seed_lng
        )

        started = time.monotonic()
        try:
            while True:
                lat, lng = _walk(args.base_lat, args.base_lng, args.radius)
                await _ping(
                    client,
                    base_url,
                    headers,
                    args.visit_id,
                    lat,
                    lng,
                    args.device_id,
                )
                if args.duration is not None and (time.monotonic() - started) >= args.duration:
                    break
                await asyncio.sleep(max(args.interval, 1.0))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\n[pinger] Ctrl-C received — stopping", flush=True)
        finally:
            try:
                await _stop_sharing(client, base_url, headers, args.visit_id)
            except Exception as exc:
                print(
                    f"[pinger] best-effort stop failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    asyncio.run(run(_parse_args()))
