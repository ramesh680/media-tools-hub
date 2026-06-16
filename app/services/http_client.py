from __future__ import annotations

import requests


class HttpClient:
    def __init__(self, user_agent: str, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout_seconds, allow_redirects=True)
        response.raise_for_status()
        return response.text

    def download(self, url: str, destination, chunk_size: int = 1024 * 1024) -> None:
        with self.session.get(url, timeout=self.timeout_seconds, stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            with open(destination, "wb") as handle:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        handle.write(chunk)
