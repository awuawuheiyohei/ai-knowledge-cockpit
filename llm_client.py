"""
llm_client.py — OpenAI-compatible Chat Completions client.

Supports any endpoint that follows the OpenAI API schema:
GLM, Taotoken, OpenAI, Azure, etc.
"""

import json
import os
import time
from urllib import request, error


class LLMClient:
    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.getenv('OPENAI_API_KEY', '')
        self.base_url = (
            base_url or os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
        ).rstrip('/')
        self.model = model or os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
        if not self.api_key:
            raise ValueError('Missing OPENAI_API_KEY — set it in .env or export it')

    def chat(self, prompt, temperature=0.3, max_retries=2):
        """Send a single-turn chat completion request with retry logic."""
        if self.base_url.endswith('/chat/completions'):
            url = self.base_url
        else:
            url = self.base_url + '/chat/completions'

        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': 'You are a helpful study copilot.'},
                {'role': 'user', 'content': prompt},
            ],
            'temperature': temperature,
        }
        data = json.dumps(payload).encode('utf-8')

        last_error = None
        for attempt in range(max_retries + 1):
            req = request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('Authorization', f'Bearer {self.api_key}')

            try:
                with request.urlopen(req, timeout=120) as resp:
                    obj = json.loads(resp.read().decode('utf-8'))

                    # Check for API-level errors
                    if 'error' in obj:
                        raise RuntimeError(f"API error: {obj['error']}")

                    # Safely extract content
                    choices = obj.get('choices', [])
                    if not choices:
                        raise RuntimeError(
                            f'No choices in API response. Full response: {json.dumps(obj)[:300]}'
                        )

                    content = choices[0].get('message', {}).get('content')
                    if content is None:
                        raise RuntimeError(
                            f'No content in API response. Full response: {json.dumps(obj)[:300]}'
                        )
                    return content

            except error.HTTPError as e:
                detail = e.read().decode('utf-8', errors='ignore')
                if e.code >= 500 and attempt < max_retries:
                    wait = 2 ** attempt
                    print(f'  ⚠  HTTP {e.code}, retrying in {wait}s...', file=__import__('sys').stderr)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f'HTTPError {e.code}: {detail}')

            except (ConnectionError, TimeoutError, OSError) as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f'  ⚠  Network error ({type(e).__name__}), retrying in {wait}s...',
                          file=__import__('sys').stderr)
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f'LLM request failed after {max_retries + 1} attempts: {e}'
                )

        raise RuntimeError(f'LLM request failed: {last_error}')
