# Copyright 2023 LiveKit, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Service for fetching live streams from pump.fun for integration testing."""

import random
import logging
from typing import Optional, Dict, Any, List

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

logger = logging.getLogger(__name__)


class PumpFunService:
    """Service for interacting with pump.fun APIs to get live streams and tokens."""

    # Constants from pump.fun
    LIVEKIT_URL = "wss://pump-prod-tg2x8veh.livekit.cloud"
    JOIN_API_URL = "https://livestream-api.pump.fun/livestream/join"
    LIVE_STREAMS_API_URL = "https://frontend-api-v3.pump.fun/coins/currently-live"

    def __init__(self) -> None:
        """Initialize the PumpFun service."""
        if not HAS_HTTPX:
            raise ImportError(
                "httpx is required for pump.fun integration. Install with: pip install httpx"
            )
        
        self.http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Origin": "https://pump.fun",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
            },
        )

    async def get_livestream_token(
        self, mint_id: str, role: str = "viewer"
    ) -> Optional[str]:
        """Get LiveKit token for a specific mint_id from pump.fun.

        Args:
            mint_id: The mint ID of the coin/stream
            role: Role to join as (default: "viewer")

        Returns:
            LiveKit token string or None if failed
        """
        try:
            payload = {"mintId": mint_id, "role": role}

            logger.info(f"Requesting token for mint_id: {mint_id}")
            response = await self.http_client.post(
                self.JOIN_API_URL,
                json=payload,
            )

            if response.status_code in [200, 201]:
                data = response.json()
                token = data.get("token")
                if token:
                    logger.info(f"Successfully obtained token for {mint_id}")
                    return token
                else:
                    logger.error(
                        f"No token in response for {mint_id}: {data}"
                    )
            else:
                logger.error(
                    f"Failed to get token for {mint_id}: "
                    f"{response.status_code} - {response.text}"
                )

        except Exception as e:
            logger.error(f"Error getting token for {mint_id}: {e}")

        return None

    async def get_currently_live_streams(
        self,
        offset: int = 0,
        limit: int = 60,
        include_nsfw: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get currently live streams from pump.fun.

        Args:
            offset: Pagination offset
            limit: Number of results to return
            include_nsfw: Whether to include NSFW streams

        Returns:
            List of live stream data
        """
        try:
            params = {
                "offset": offset,
                "limit": limit,
                "sort": "currently_live",
                "order": "DESC",
                "includeNsfw": str(include_nsfw).lower(),
            }

            logger.info(f"Fetching live streams with params: {params}")
            response = await self.http_client.get(
                self.LIVE_STREAMS_API_URL,
                params=params,
            )

            if response.status_code == 200:
                streams = response.json()
                logger.info(f"Found {len(streams)} live streams")

                # Filter to only currently live streams
                live_streams = [
                    stream
                    for stream in streams
                    if stream.get("is_currently_live", False)
                ]

                logger.info(
                    f"Filtered to {len(live_streams)} currently live streams"
                )
                return live_streams
            else:
                logger.error(
                    f"Failed to get live streams: "
                    f"{response.status_code} - {response.text}"
                )

        except Exception as e:
            logger.error(f"Error getting live streams: {e}")

        return []

    async def get_random_live_stream(
        self,
        min_participants: int = 0,
        exclude_nsfw: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Get a random live stream from pump.fun.

        Args:
            min_participants: Minimum number of participants required
            exclude_nsfw: Whether to exclude NSFW streams

        Returns:
            Random live stream data or None if none found
        """
        try:
            streams = await self.get_currently_live_streams(
                limit=60, include_nsfw=not exclude_nsfw
            )

            if not streams:
                logger.warning("No live streams found")
                return None

            # Filter by minimum participants
            if min_participants > 0:
                streams = [
                    s
                    for s in streams
                    if s.get("num_participants", 0) >= min_participants
                ]

            if not streams:
                logger.warning(
                    f"No streams found with at least {min_participants} participants"
                )
                return None

            # Pick a random stream
            selected = random.choice(streams)
            mint_id = selected.get("mint")
            
            if not mint_id:
                logger.error("Selected stream has no mint ID")
                return None

            logger.info(
                f"Selected stream: {selected.get('name', 'Unknown')} "
                f"({selected.get('symbol', 'N/A')}) - "
                f"{selected.get('num_participants', 0)} participants"
            )

            return selected

        except Exception as e:
            logger.error(f"Error getting random live stream: {e}")
            return None

    async def get_stream_info(self, mint_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific stream by mint_id.

        Args:
            mint_id: The mint ID to look for

        Returns:
            Stream info dict or None if not found
        """
        try:
            streams = await self.get_currently_live_streams(limit=100)

            for stream in streams:
                if stream.get("mint") == mint_id:
                    logger.info(
                        f"Found stream info for {mint_id}: "
                        f"{stream.get('name')} ({stream.get('symbol')})"
                    )
                    return stream

            logger.warning(f"Stream not found for mint_id: {mint_id}")

        except Exception as e:
            logger.error(f"Error getting stream info for {mint_id}: {e}")

        return None

    def get_livekit_url(self) -> str:
        """Get the constant LiveKit URL for pump.fun."""
        return self.LIVEKIT_URL

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.http_client.aclose()

