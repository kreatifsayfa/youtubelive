#!/usr/bin/env python3
"""
M3U Playlist Parser
Parses M3U/M3U8 playlist files and extracts channel information.
"""

import re
import requests
from typing import List, Dict, Optional, Tuple


class M3UParser:
    """Parser for M3U playlist files."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def fetch_playlist(self, url: str) -> Optional[str]:
        """Fetch M3U playlist content from URL."""
        try:
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            # Handle potential encoding issues
            response.encoding = 'utf-8'
            return response.text
        except requests.RequestException as e:
            raise Exception(f"Failed to fetch playlist: {str(e)}")

    def parse(self, content: str) -> List[Dict[str, str]]:
        """
        Parse M3U playlist content and return list of channels.

        Returns:
            List of channel dictionaries with keys:
            - id: unique identifier
            - name: channel name
            - url: stream URL
            - group: category/group title
            - logo: channel logo URL
            - duration: duration in seconds (if available)
        """
        channels = []
        lines = content.strip().split('\n')

        # Check if it's a valid M3U file
        if not lines or not lines[0].strip().startswith('#EXTM3U'):
            raise Exception("Invalid M3U file: Missing #EXTM3U header")

        current_channel = {}
        channel_counter = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse #EXTINF entry
            if line.startswith('#EXTINF:'):
                # Reset current channel
                current_channel = {}

                # Parse EXTINF attributes
                # Format: #EXTINF:-1 tvg-id="xxx" tvg-name="xxx" tvg-logo="xxx" group-title="xxx",Channel Name
                extinf_match = re.match(r'#EXTINF:(-?\d+)\s+(.*)', line)
                if extinf_match:
                    duration = extinf_match.group(1)
                    attrs_part = extinf_match.group(2)

                    current_channel['duration'] = duration

                    # Parse attributes before comma
                    if ',' in attrs_part:
                        attr_str, channel_name = attrs_part.rsplit(',', 1)
                        current_channel['name'] = channel_name.strip()

                        # Parse key="value" attributes
                        attr_pattern = r'(\w+)="([^"]*)"'
                        for match in re.finditer(attr_pattern, attr_str):
                            key, value = match.groups()
                            if key == 'tvg-id':
                                current_channel['tvg_id'] = value
                            elif key == 'tvg-name':
                                current_channel['tvg_name'] = value
                            elif key == 'tvg-logo':
                                current_channel['logo'] = value
                            elif key == 'group-title':
                                current_channel['group'] = value
                    else:
                        # No attributes, just duration
                        current_channel['name'] = attrs_part.strip() if attrs_part else 'Unknown Channel'
                else:
                    # Simple format: #EXTINF:-1,Channel Name
                    if ',' in line:
                        _, channel_name = line.split(',', 1)
                        current_channel['name'] = channel_name.strip()
                    else:
                        current_channel['name'] = 'Unknown Channel'

            # Parse URL line (non-comment line)
            elif not line.startswith('#'):
                if current_channel and 'name' in current_channel:
                    current_channel['url'] = line
                    channel_counter += 1
                    current_channel['id'] = f'ch_{channel_counter}'

                    # Set defaults for missing fields
                    if 'group' not in current_channel:
                        current_channel['group'] = 'Genel'
                    if 'logo' not in current_channel:
                        current_channel['logo'] = ''

                    channels.append(current_channel.copy())
                    current_channel = {}
                elif current_channel is None:
                    # URL without EXTINF (simple format)
                    channel_counter += 1
                    channels.append({
                        'id': f'ch_{channel_counter}',
                        'name': f'Channel {channel_counter}',
                        'url': line,
                        'group': 'Genel',
                        'logo': ''
                    })

        return channels

    def parse_from_url(self, url: str) -> Tuple[List[Dict[str, str]], str]:
        """
        Fetch and parse M3U playlist from URL.

        Returns:
            Tuple of (channels list, raw content for caching)
        """
        content = self.fetch_playlist(url)
        channels = self.parse(content)
        return channels, content

    def filter_channels(self, channels: List[Dict[str, str]], search: str = '',
                        group: str = '') -> List[Dict[str, str]]:
        """
        Filter channels by search term and/or group.

        Args:
            channels: List of channel dictionaries
            search: Search term to filter by name
            group: Group/category to filter by

        Returns:
            Filtered list of channels
        """
        filtered = channels

        if group:
            filtered = [c for c in filtered if c.get('group', '').lower() == group.lower()]

        if search:
            search_lower = search.lower()
            filtered = [c for c in filtered if search_lower in c.get('name', '').lower()]

        return filtered

    def get_groups(self, channels: List[Dict[str, str]]) -> List[str]:
        """Get unique list of group titles from channels."""
        groups = set()
        for channel in channels:
            if 'group' in channel and channel['group']:
                groups.add(channel['group'])
        return sorted(list(groups))


def parse_playlist_from_url(url: str, timeout: int = 30) -> Dict:
    """
    Convenience function to parse M3U playlist from URL.

    Returns:
        Dictionary with:
        - success: bool
        - channels: list of channel dicts
        - count: number of channels
        - groups: list of unique group names
        - error: error message if failed
    """
    parser = M3UParser(timeout=timeout)
    try:
        channels, content = parser.parse_from_url(url)
        groups = parser.get_groups(channels)

        return {
            'success': True,
            'channels': channels,
            'count': len(channels),
            'groups': groups
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'channels': [],
            'count': 0,
            'groups': []
        }
