import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from rest_framework.exceptions import APIException


class WeatherToolError(APIException):
    status_code = 502
    default_detail = 'The weather service could not provide a forecast.'
    default_code = 'weather_service_error'


class WeatherLocationRequired(APIException):
    status_code = 400
    default_detail = 'Include a location, for example: "What is the weather in Boston?"'
    default_code = 'weather_location_required'


WEATHER_CODES = {
    0: 'clear sky',
    1: 'mainly clear',
    2: 'partly cloudy',
    3: 'overcast',
    45: 'fog',
    48: 'depositing rime fog',
    51: 'light drizzle',
    53: 'moderate drizzle',
    55: 'dense drizzle',
    61: 'slight rain',
    63: 'moderate rain',
    65: 'heavy rain',
    71: 'slight snow',
    73: 'moderate snow',
    75: 'heavy snow',
    80: 'rain showers',
    81: 'moderate rain showers',
    82: 'violent rain showers',
    95: 'thunderstorm',
    96: 'thunderstorm with hail',
    99: 'thunderstorm with heavy hail',
}


def _get_json(url, params):
    request = Request(
        f'{url}?{urlencode(params)}',
        headers={'User-Agent': 'DocumentAssistant/1.0'},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        raise WeatherToolError(
            f'The weather service returned HTTP {error.code}.'
        ) from error
    except (URLError, TimeoutError) as error:
        raise WeatherToolError('The weather service could not be reached.') from error
    except (UnicodeDecodeError, ValueError) as error:
        raise WeatherToolError() from error


def _location_from_query(query):
    match = re.search(
        r'\b(?:in|for|at)\s+([A-Za-z][A-Za-z .\'-]{1,60})',
        query,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise WeatherLocationRequired()
    location = re.sub(
        r'\b(today|tomorrow|right now|currently|this week|please)\b.*$',
        '',
        match.group(1),
        flags=re.IGNORECASE,
    ).strip(' .?!,')
    if not location:
        raise WeatherLocationRequired()
    return location


def fetch_current_weather(query):
    location = _location_from_query(query)
    geocoding = _get_json(
        'https://geocoding-api.open-meteo.com/v1/search',
        {'name': location, 'count': 1, 'language': 'en', 'format': 'json'},
    )
    if not isinstance(geocoding, dict):
        raise WeatherToolError()
    matches = geocoding.get('results') or []
    if not matches:
        raise WeatherToolError(f'No weather location matched "{location}".')
    try:
        place = matches[0]
        forecast = _get_json(
            'https://api.open-meteo.com/v1/forecast',
            {
                'latitude': place['latitude'],
                'longitude': place['longitude'],
                'current': (
                    'temperature_2m,relative_humidity_2m,apparent_temperature,'
                    'precipitation,weather_code,wind_speed_10m'
                ),
                'daily': (
                    'weather_code,temperature_2m_max,temperature_2m_min,'
                    'precipitation_probability_max'
                ),
                'forecast_days': 2,
                'timezone': 'auto',
            },
        )
        current = forecast['current']
        units = forecast['current_units']
        place_name = ', '.join(
            part for part in (
                place.get('name'),
                place.get('admin1'),
                place.get('country'),
            ) if part
        )
        if re.search(r'\btomorrow\b', query, flags=re.IGNORECASE):
            daily = forecast['daily']
            condition = WEATHER_CODES.get(
                daily['weather_code'][1],
                'unknown conditions',
            )
            return (
                f'Tomorrow forecast for {place_name} ({daily["time"][1]}): '
                f'{condition}; high {daily["temperature_2m_max"][1]} '
                f'{forecast["daily_units"]["temperature_2m_max"]}, '
                f'low {daily["temperature_2m_min"][1]} '
                f'{forecast["daily_units"]["temperature_2m_min"]}, '
                f'max precipitation probability '
                f'{daily["precipitation_probability_max"][1]}%. '
                'Source: Open-Meteo.'
            )
        condition = WEATHER_CODES.get(current.get('weather_code'), 'unknown conditions')
        details = (
            f'{current["temperature_2m"]} {units["temperature_2m"]}, '
            f'feels like {current["apparent_temperature"]} '
            f'{units["apparent_temperature"]}, '
            f'humidity {current["relative_humidity_2m"]}%, '
            f'wind {current["wind_speed_10m"]} {units["wind_speed_10m"]}, '
            f'precipitation {current["precipitation"]} '
            f'{units["precipitation"]}. '
            f'Observed at {current["time"]}. Source: Open-Meteo.'
        )
    except (AttributeError, KeyError, IndexError, TypeError) as error:
        raise WeatherToolError() from error
    return (
        f'Current weather for {place_name}: {condition}; {details}'
    )
