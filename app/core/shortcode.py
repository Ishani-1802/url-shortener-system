import random
import string

# The Base62 alphabet — every character is URL-safe, no encoding needed
ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
# '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
BASE = len(ALPHABET)  # 62

# How long each short code is
# 62^7 = 3,521,614,606,208 unique codes — more than enough
CODE_LENGTH = 7

# Max retries before giving up on collision resolution
MAX_RETRIES = 5


def generate_short_code(length: int = CODE_LENGTH) -> str:
    """
    Generate a cryptographically random Base62 short code.

    Uses random.choices() which is fast and uniform.
    For security-critical tokens, use secrets.choice() instead.
    For a URL shortener, random.choices() is perfectly fine.

    Example output: 'aB3xK9m', 'Z0qR7pL'
    """
    return "".join(random.choices(ALPHABET, k=length))


def encode_base62(number: int) -> str:
    """
    Encode a positive integer to Base62 string.
    Used in the counter-based approach (Snowflake-style).

    Example:
        encode_base62(1_000_000) → '4c92'
        encode_base62(0)         → '0'

    This is the alternative approach — encode a global counter
    instead of generating random codes. Zero collisions guaranteed,
    but requires a reliable distributed counter (Redis INCR).
    """
    if number == 0:
        return ALPHABET[0]

    result = []
    while number:
        number, remainder = divmod(number, BASE)
        result.append(ALPHABET[remainder])

    return "".join(reversed(result))


def decode_base62(code: str) -> int:
    """
    Decode a Base62 string back to integer.
    Useful for debugging and analytics.

    Example:
        decode_base62('4c92') → 1_000_000
    """
    result = 0
    for char in code:
        result = result * BASE + ALPHABET.index(char)
    return result