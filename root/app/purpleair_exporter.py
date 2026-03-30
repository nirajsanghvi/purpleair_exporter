import logging
import os
import sys
import time
import requests
from typing import Iterator
from prometheus_client import Counter, Gauge, start_http_server

DEFAULT_RUN_INTERVAL_SECONDS = 120
V1_API_ENDPOINT = "https://api.purpleair.com/v1"
API_SENSOR_FIELDS = ["name","last_seen","pm2.5","pm2.5_10minute","pm10.0","temperature","pressure","humidity"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("purpleair_exporter")

# Stats
STAT_PREFIX = "purpleair_"
SENSOR_LABELS = ["sensor_id", "label"]
AQI_LABELS = SENSOR_LABELS + ["conversion"]

# Metrics
FetchErrors = Counter(STAT_PREFIX + "fetch_errors", "Errors fetching data from PurpleAir sensor")

Pm2_5 = Gauge(STAT_PREFIX + "pm2_5", "2.5 micron particulate matter (ug/m^3)", SENSOR_LABELS)
Pm2_5_10_minute = Gauge(STAT_PREFIX + "pm2_5_10_minute", "2.5 micron particulate matter (ug/m^3) 10 minute average", SENSOR_LABELS)
Pm10 = Gauge(STAT_PREFIX + "pm10_0", "10 micron particulate matter (ug/m^3)", SENSOR_LABELS)

Aqi2_5 = Gauge(STAT_PREFIX + "aqi_pm2_5", "PM2.5 AQI", AQI_LABELS)
Aqi2_5_10Minute = Gauge(STAT_PREFIX + "aqi_pm2_5_10_minute", "PM2.5 AQI 10-minute", AQI_LABELS)
Aqi10 = Gauge(STAT_PREFIX + "aqi_pm10_0", "PM10 AQI", AQI_LABELS)

Temp_f = Gauge(STAT_PREFIX + "temp_f", "Temperature in degrees Fahrenheit", SENSOR_LABELS)
Humidity = Gauge(STAT_PREFIX + "humidity", "% Humidity", SENSOR_LABELS)
Pressure = Gauge(STAT_PREFIX + "pressure", "Pressure in millibar", SENSOR_LABELS)
LastSeen = Gauge(STAT_PREFIX + "last_seen_seconds", "timestamp when this sensor was last seen", SENSOR_LABELS)

# API field names to metric names
SENSOR_MAP = {
    "pm2.5": Pm2_5,
    "pm2.5_10minute": Pm2_5_10_minute,
    "pm10.0": Pm10,
    "temperature": Temp_f,
    "pressure": Pressure,
    "humidity": Humidity,
    "last_seen": LastSeen,
}


def main() -> None:
    log_level = os.environ.get("PAE_LOGGING", "info")
    prom_port = int(os.environ.get("PAE_PROM_PORT", "9101"))

    try:
        run_interval_s = int(os.environ.get("PAE_RUN_INTERVAL_S", DEFAULT_RUN_INTERVAL_SECONDS))
    except ValueError:
        logger.error(f"Invalid env var: PAE_RUN_INTERVAL_S must be an integer")
        sys.exit(1)

    if "PAE_SENSOR_IDS" not in os.environ or not os.environ["PAE_SENSOR_IDS"]:
        logger.error(f"Missing env var: PAE_SENSOR_IDS")
        sys.exit(1)

    api_key = os.environ.get("PAE_API_READ_KEY", "")
    if api_key == "":
        logger.error("Missing env var: PAE_API_READ_KEY")
        sys.exit(1)

    validate_api_key(api_key)

    sensor_ids = os.environ["PAE_SENSOR_IDS"].replace(" ", "")

    log_level = getattr(logging, log_level.upper())
    logger.setLevel(log_level)

    start_http_server(int(prom_port))

    for _ in Ticker(run_interval_s).run():
        collect_metrics(sensor_ids, api_key)


def collect_metrics(sensor_ids, api_key) -> None:
    logger.info(f"Collecting metrics for sensors: {sensor_ids}")
    api_data = api_get_sensors(sensor_ids, api_key)
    if api_data:
        fields = api_data["fields"]
        sensors_data = api_data["data"]
        for sensor_data in sensors_data:
            parsed_data = parse_sensor_data(sensor_data, fields)
            transform_sensor_data(parsed_data)
    

def parse_sensor_data(sensor_data, fields):
    parsed_data = {}
    for i, field in enumerate(fields):
        parsed_data[field] = sensor_data[i]
    return parsed_data


def api_get_sensors(sensor_ids, api_key):
    url = f"{V1_API_ENDPOINT}/sensors"
    headers = {"X-API-Key": api_key}
    params = {
        "fields": ",".join(API_SENSOR_FIELDS),
        "show_only": sensor_ids
    }

    response = requests.get(url, params=params, headers=headers)

    logger.debug(f"API response: {response.status_code}; {response.text}")

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            logger.error(f"Invalid JSON response from API: {response.text}")
            FetchErrors.inc()
            return None

        ''' 
        Example API response:
            {
                ...
                "fields" : ["sensor_index","last_seen","name","humidity","temperature","pressure","pm2.5","pm10.0","pm2.5_10minute"],
                "data" : [
                    [37143,1681674052,"Oak Yard",58,67,1016.22,2.0,2.7,1.6],
                    [39773,1681673994,"Oak Inside",25,78,1016.14,1.1,1.8,0.8]
                ]
            }
        '''

        if 'data' not in data or len(data['data']) == 0:
            logger.error(f"No data returned for sensors: {sensor_ids}")
            return None
                         
        if 'fields' not in data:
            logger.error(f"No fields returned for sensors: {sensor_ids}")
            return None

        if len(data['fields']) != len(data['data'][0]):
            logger.error(f"Number of fields does not match number of data points for sensors: {sensor_ids}")
            return None

        return data
    else:
        print(f"Error fetching data for sensors: {response.status_code}")
        FetchErrors.inc()
        return None


def transform_sensor_data(data):
    sensor_id = data.get("sensor_index", "")
    sensor_label = data.get("name", "")

    for key, stat in SENSOR_MAP.items():
        if key in data and data[key] is not None:
            stat.labels(sensor_id = sensor_id, label = sensor_label).set(data[key])

    # Calculate AQI for PM2.5, PM2.5_10minute, and PM10
    if "pm2.5" in data and data["pm2.5"] is not None:
        aqi = aqiFromPM(float(data["pm2.5"]))
        Aqi2_5.labels(sensor_id = sensor_id, label = sensor_label, conversion="None").set(aqi)

        aqi = aqiFromPM(aqandu(float(data["pm2.5"])))
        Aqi2_5.labels(sensor_id = sensor_id, label = sensor_label, conversion="AQandU").set(aqi)

    if "pm2.5_10minute" in data and data["pm2.5_10minute"] is not None:
        aqi = aqiFromPM(float(data["pm2.5_10minute"]))
        Aqi2_5_10Minute.labels(sensor_id = sensor_id, label = sensor_label, conversion="None").set(aqi)

        aqi = aqiFromPM(aqandu(float(data["pm2.5_10minute"])))
        Aqi2_5_10Minute.labels(sensor_id = sensor_id, label = sensor_label, conversion="AQandU").set(aqi)

    if "pm10.0" in data and data["pm10.0"] is not None:
        aqi = aqiFromPM(float(data["pm10.0"]))
        Aqi10.labels(sensor_id = sensor_id, label = sensor_label, conversion="None").set(aqi)

        aqi = aqiFromPM(aqandu(float(data["pm10.0"])))
        Aqi10.labels(sensor_id = sensor_id, label = sensor_label, conversion="AQandU").set(aqi)


def validate_api_key(api_key: str) -> None:
    logger.info("Validating API read key")
    url = f"{V1_API_ENDPOINT}/keys"
    headers = {"X-API-Key": api_key}

    response = requests.get(url, headers=headers)

    # For some reason the API returns 201 instead of 200 on a key check, so just look for both status codes to indicate success
    if response.status_code != 200 and response.status_code != 201:
        logger.error(f"Invalid API key: {api_key}. \
                     Make sure you are providing a read key generated from https://develop.purpleair.com/keys")
        sys.exit(1)
    
    try:
        response_json = response.json()
    except requests.exceptions.JSONDecodeError:
        logger.error(f"Key check did not return a valid JSON response")
        sys.exit(1)

    key_type = response_json["api_key_type"]
    if key_type != "READ":
        logger.error(f"The given API key: {api_key} was not a read key, it was a {key_type} key. \
                     Make sure you are providing a read key generated from https://develop.purpleair.com/keys")
        sys.exit(1)


# Convert US AQI from raw pm2.5 data
# Code from https://community.purpleair.com/t/how-to-calculate-the-us-epa-pm2-5-aqi/877/11
def aqiFromPM(pm):
    if not float(pm) and pm != 0.0:
        return "-"
    if pm == 'undefined':
        return "-"
    if pm < 0:
        return pm
    if pm > 1000:
        return "-"
    """
                                        AQI   | RAW PM2.5    
    Good                               0 - 50 | 0.0 - 12.0    
    Moderate                         51 - 100 | 12.1 - 35.4
    Unhealthy for Sensitive Groups  101 - 150 | 35.5 - 55.4
    Unhealthy                       151 - 200 | 55.5 - 150.4
    Very Unhealthy                  201 - 300 | 150.5 - 250.4
    Hazardous                       301 - 400 | 250.5 - 350.4
    Hazardous                       401 - 500 | 350.5 - 500.4
    """

    if pm > 350.5:
        return calcAQI(pm, 500, 401, 500.4, 350.5)  # Hazardous
    elif pm > 250.5:
        return calcAQI(pm, 400, 301, 350.4, 250.5)  # Hazardous
    elif pm > 150.5:
        return calcAQI(pm, 300, 201, 250.4, 150.5)  # Very Unhealthy
    elif pm > 55.5:
        return calcAQI(pm, 200, 151, 150.4, 55.5)  # Unhealthy
    elif pm > 35.5:
        return calcAQI(pm, 150, 101, 55.4, 35.5)  # Unhealthy for Sensitive Groups
    elif pm > 12.1:
        return calcAQI(pm, 100, 51, 35.4, 12.1)  # Moderate
    elif pm >= 0:
        return calcAQI(pm, 50, 0, 12, 0)  # Good
    else:
        return 'undefined'


# Calculate AQI from standard ranges
def calcAQI(Cp, Ih, Il, BPh, BPl):
    a = (Ih - Il)
    b = (BPh - BPl)
    c = (Cp - BPl)
    calc = round((a / b) * c + Il)
    if calc > 500:
        calc = 500
    return calc


# AQandU formula info: https://community.purpleair.com/t/the-apply-conversion-field/103
def aqandu(pm: float) -> float:
    return 0.778 * pm + 2.65


class Ticker:
    def __init__(self, interval: float):
        self.interval = interval
        self.go = True

    def stop(self) -> None:
        self.go = False

    def run(self) -> Iterator[bool]:
        logger.debug(f"Ticker running every {self.interval} seconds")
        while self.go:
            logger.debug("tick")
            start = time.time()
            yield True
            end = time.time()
            duration = end - start

            sleep_time = self.interval - duration
            if sleep_time < 0:
                logger.warning(f"Iteration took longer than {self.interval} seconds")
                sleep_time = 0
            logger.info(f"Sleeping for {sleep_time} seconds")
            time.sleep(sleep_time)


if __name__ == "__main__":
    main()
