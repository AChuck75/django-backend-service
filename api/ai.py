import hashlib
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from rest_framework.exceptions import APIException


class AIServiceUnavailable(APIException):
    status_code = 503
    default_detail = 'The AI provider is not configured.'
    default_code = 'ai_service_unavailable'


class AIProviderError(APIException):
    status_code = 502
    default_detail = 'The AI provider returned an invalid response.'
    default_code = 'ai_provider_error'


def _post_claude(payload):
    if not settings.ANTHROPIC_API_KEY:
        raise AIServiceUnavailable()

    url = f'{settings.ANTHROPIC_BASE_URL.rstrip("/")}/v1/messages'
    request = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'x-api-key': settings.ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=settings.AI_PROVIDER_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        raise AIProviderError(
            f'The AI provider request failed with status {error.code}.'
        ) from error
    except (URLError, TimeoutError) as error:
        raise AIProviderError('The AI provider could not be reached.') from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AIProviderError() from error


def generate_answer(system_prompt, user_prompt):
    result = _post_claude(
        {
            'model': settings.CLAUDE_MODEL,
            'max_tokens': settings.AI_MAX_RESPONSE_TOKENS,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': user_prompt}],
        },
    )
    try:
        answer = ''.join(
            block['text']
            for block in result['content']
            if block.get('type') == 'text' and isinstance(block.get('text'), str)
        )
    except (AttributeError, KeyError, TypeError) as error:
        raise AIProviderError() from error
    if not isinstance(answer, str) or not answer.strip():
        raise AIProviderError()
    return answer.strip()


def create_embeddings(texts):
    return [_local_embedding(text) for text in texts]


def _local_embedding(text):
    vector = [0.0] * settings.LOCAL_EMBEDDING_DIMENSIONS
    tokens = re.findall(r'\w+', text.casefold())
    features = tokens + [
        f'{left}_{right}'
        for left, right in zip(tokens, tokens[1:])
    ]
    for feature in features:
        digest = hashlib.blake2b(feature.encode('utf-8'), digest_size=8).digest()
        value = int.from_bytes(digest, byteorder='big')
        index = value % settings.LOCAL_EMBEDDING_DIMENSIONS
        vector[index] += 1.0 if value & (1 << 63) else -1.0
    return vector


def cosine_similarity(left, right):
    if len(left) != len(right):
        return 0
    dot_product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0
    return dot_product / (left_norm * right_norm)
