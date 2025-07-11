from typing import List, Literal, Optional, Tuple
import logging
import re

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .schema import Flight, Result
from .flights_impl import FlightData, Passengers
from .filter import TFSData
from .fallback_playwright import fallback_playwright_fetch
from .primp import Client, Response

logger = logging.getLogger(__name__)

class FlightParsingError(RuntimeError):
    """Raised when flight data cannot be parsed"""
    pass


class FlightDateTooFarError(RuntimeError):
    """Raised when the requested flight date is too far in the future"""
    pass


def fetch(params: dict, proxy: Optional[str] = None) -> Response:
    client = Client(impersonate="chrome_126", verify=False, proxy=proxy)
    res = client.get("https://www.google.com/travel/flights", params=params, cookies={
        # EU cookies to bypass the data collection form
        "CONSENT": "PENDING+987",
        "SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmRlIAEaBgiAo_CmBg"
    })
    assert res.status_code == 200, f"{res.status_code} Result: {res.text_markdown}"
    return res


def get_flights_from_filter(
    filter: TFSData,
    currency: str = "",
    *,
    mode: Literal["common", "fallback", "force-fallback", "local"] = "common",
    proxy: Optional[str] = None,
) -> Result:
    data = filter.as_b64()

    params = {
        "tfs": data.decode("utf-8"),
        "hl": "en",
        "tfu": "EgQIABABIgA",
        "curr": currency,
    }

    if mode in {"common", "fallback"}:
        try:
            res = fetch(params, proxy=proxy)
        except AssertionError as e:
            if mode == "fallback":
                res = fallback_playwright_fetch(params)
            else:
                raise e

    elif mode == "local":
        from .local_playwright import local_playwright_fetch

        res = local_playwright_fetch(params)

    else:
        res = fallback_playwright_fetch(params)

    try:
        return parse_response(res)
    except RuntimeError as e:
        logger.debug(f"RuntimeError in get_flights_from_filter: {e}")
        if mode == "fallback":
            return get_flights_from_filter(filter, mode="local", proxy=proxy)
        raise e


def get_flights(
    *,
    flight_data: List[FlightData],
    trip: Literal["round-trip", "one-way", "multi-city"],
    passengers: Passengers,
    seat: Literal["economy", "premium-economy", "business", "first"],
    fetch_mode: Literal["common", "fallback", "force-fallback", "local"] = "common",
    max_stops: Optional[int] = None,
) -> Result:
    return get_flights_from_filter(
        TFSData.from_interface(
            flight_data=flight_data,
            trip=trip,
            passengers=passengers,
            seat=seat,
            max_stops=max_stops,
        ),
        mode=fetch_mode,
    )


def parse_response(
    r: Response, *, dangerously_allow_looping_last_item: bool = False
) -> Result:
    class _blank:
        def text(self, *_, **__):
            return ""

        def iter(self):
            return []

    blank = _blank()

    def safe(n: Optional[LexborNode]):
        return n or blank
    
    def extract_airline_code_and_flight_number(s: str) -> Optional[List[Tuple[str, str]]]:
        """Extract airline code and flight number from string"""
        pattern = r"-([A-Z0-9]{2})-(\d{2,4})-"
        result = re.findall(pattern, s)
        if not result:
            print("No airline code and flight number found in:", s)
        return result[0][0], result[0][1]

    parser = LexborHTMLParser(r.text)
    
    # Check for flight date too far in future error message
    if parser.css_first('div[jsname="qJTHM"][class="FXkZv fXx9Lc"]'):
        raise FlightDateTooFarError("Requested flight date is too far in the future")
    
    # Check for required qJTHM element - should exist even if no flights
    if not parser.css_first('div[jsname="qJTHM"]'):
        raise FlightParsingError("Required qJTHM element not found in response")
    
    flights = []

    for i, fl in enumerate(parser.css('div[jsname="IWWDBc"], div[jsname="YdtKid"]')):
        is_best_flight = i == 0

        for item in fl.css("ul.Rk10dc li")[
            : (None if dangerously_allow_looping_last_item or i == 0 else -1)
        ]:
            # Flight name
            name = safe(item.css_first("div.sSHqwe.tPgKwe.ogfYpf span")).text(
                strip=True
            )

            # Get departure & arrival time
            dp_ar_node = item.css("span.mv1WYe div")
            try:
                departure_time = dp_ar_node[0].text(strip=True)
                arrival_time = dp_ar_node[1].text(strip=True)
            except IndexError:
                # sometimes this is not present
                departure_time = ""
                arrival_time = ""

            # Get arrival time ahead
            time_ahead = safe(item.css_first("span.bOzv6")).text()

            # Get duration
            duration = safe(item.css_first("li div.Ak5kof div")).text()

            # Get flight stops
            stops = safe(item.css_first(".BbR8Ec .ogfYpf")).text()

            # Get delay
            delay = safe(item.css_first(".GsCCve")).text() or None

            # Get prices
            price = safe(item.css_first(".YMlIz.FpEdX")).text() or "0"

            # Stops formatting
            try:
                stops_fmt = 0 if stops == "Nonstop" else int(stops.split(" ", 1)[0])
            except ValueError:
                stops_fmt = "Unknown"

            try:
                airline_code, flight_number = extract_airline_code_and_flight_number(
                    item.css_first(".NZRfve").attributes["data-travelimpactmodelwebsiteurl"])
            except ValueError:
                raise FlightParsingError("Can't parse airline code or flight number")

            flights.append(
                {
                    "is_best": is_best_flight,
                    "name": name,
                    "departure": " ".join(departure_time.split()),
                    "arrival": " ".join(arrival_time.split()),
                    "arrival_time_ahead": time_ahead,
                    "duration": duration,
                    "stops": stops_fmt,
                    "delay": delay,
                    "price": price.replace(",", ""),
                    "airline_code": airline_code,
                    "flight_number": flight_number,
                }
            )

    current_price = safe(parser.css_first("span.gOatQ")).text()
    #if not flights:
    #    raise NoFlightsFoundError("No flights found:\n{}".format(r.text_markdown))

    return Result(current_price=current_price, flights=[Flight(**fl) for fl in flights]) 
