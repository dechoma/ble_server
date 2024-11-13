import sys
import logging
import aiohttp
import asyncio
import threading
import os
import sdnotify

# Initialize the notifier
notifier = sdnotify.SystemdNotifier()

from typing import Any, Union

from bless import (  # type: ignore
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

from secrets import HA_AUTH, HA_ENDPOINT



import requests
from bs4 import BeautifulSoup
from datetime import datetime
import json


async def get_next_departures(url_template, lines=["197", "201"], count=3):
    # Generujemy bieżącą datę
    today = datetime.now()
    url = url_template.format(date=today.strftime("%Y-%m-%d"))

    # Pobieramy stronę
    #response = requests.get(url)
    #response.raise_for_status()
    # Fetching the page asynchronously
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            html = await response.text()

    # Tworzymy obiekt BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')

    # Słownik do przechowywania odjazdów
    departures = {line: [] for line in lines}

    # Szukamy wszystkich wpisów odjazdów
    for entry in soup.find_all('li', class_='timetable-departures-entry'):
        # Numer linii
        line_number = entry.find('a', class_='timetable-button-tile').get_text(strip=True)

        # Godzina odjazdu
        departure_time_str = entry.find('div', class_='timetable-departures-entry-hour').get_text(strip=True)

        # Próbujemy sparsować godzinę odjazdu
        try:
            departure_time = datetime.strptime(departure_time_str, '%H:%M').time()
            if line_number in departures:
                departures[line_number].append(departure_time)
        except ValueError:
            continue

    # Sortowanie i wybieranie najbliższych odjazdów
    current_time = datetime.now().time()
    nearest_departures = {}

    for line, times in departures.items():
        # Filtrujemy godziny odjazdów po czasie bieżącym i sortujemy
        future_times = sorted([t for t in times if t >= current_time])

        # Wybieramy trzy najbliższe godziny i łączymy bez odstępów przy separatorze
        formatted_times = [time.strftime('%H:%M') for time in future_times[:count]]
        nearest_departures[line] = "|".join(formatted_times)

    logger.debug(nearest_departures)
    # Konwersja wyniku na format JSON
    return nearest_departures


# Szablon URL z automatyczną datą
url_template = "https://www.wtp.waw.pl/rozklady-jazdy/?wtp_dt={date}&wtp_md=8&wtp_dy=1&wtp_st=5151&wtp_pt=02"

# Pobieramy najbliższe odjazdy
#next_departures_json = get_next_departures(url_template)


SERVER_NAME = "SPServer"  # must be shorter than 10 characters
SERVICE_UUID = "D2EA587F-19C8-4F4C-8179-3BA0BC150B01"
CHARACTERISTICS = [
    "0DF8D897-33FE-4AF4-9E7A-63D24664C94B",
    "0DF8D897-33FE-4AF4-9E7A-63D24664C94C",
    "0DF8D897-33FE-4AF4-9E7A-63D24664C94D",
    "0DF8D897-33FE-4AF4-9E7A-63D24664C94E",
    "0DF8D897-33FE-4AF4-9E7A-63D24664C94F",
]

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(name=__name__)

# NOTE: Some systems require different synchronization methods.
trigger: Union[asyncio.Event, threading.Event]
if sys.platform in ["darwin", "win32"]:
    trigger = threading.Event()
else:
    trigger = asyncio.Event()


def read_request(characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
    logger.debug(f"Reading {characteristic.value}")
    return characteristic.value


async def get_ha_data():
    headers = {
        "Authorization": HA_AUTH,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(HA_ENDPOINT, headers=headers) as response:
            return await response.text()

server = None
async def run(loop):
    trigger.clear()

    # Instantiate the server
    server = BlessServer(name=SERVER_NAME, loop=loop)
    server.read_request_func = read_request

    await server.add_new_service(SERVICE_UUID)

    char_flags = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
    permissions = GATTAttributePermissions.readable

    for uuid in CHARACTERISTICS:
        await server.add_new_characteristic(
            SERVICE_UUID, uuid, char_flags, None, permissions
        )

    await server.start()
    logger.debug("Advertising")

    while True and not trigger.is_set():
        try:
            logger.debug("Updating HA data")
            data = json.loads((await get_ha_data()))
            if not data['attributes']['bus_197']:
                next_departures_json = await get_next_departures(url_template)
                if next_departures_json:
                  data['attributes']['bus_197'] = next_departures_json['197']
                  data['attributes']['bus_201'] = next_departures_json['201']
            data = json.dumps(data).encode('utf-8')
            split_val = 240
            values = [data[i : i + split_val] for i in range(0, len(data), split_val)]
            for i, val in enumerate(values):
                server.get_characteristic(CHARACTERISTICS[i]).value = val
            logger.debug("Updated HA data")
            notifier.notify("WATCHDOG=1")
        except Exception as e:
            logger.error(e)
            sys.exit(1)
        await asyncio.sleep(60)

    await server.stop()


loop = asyncio.get_event_loop()
loop.run_until_complete(run(loop))