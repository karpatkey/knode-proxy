import collections.abc
import hashlib
import logging
import numbers
import os
from types import NoneType

import diskcache

logger = logging.getLogger("proxy.cache")

VERSION = 1
VERSION_CACHE_KEY = "VERSION"

cache_dir = os.environ.get("KPROXY_CACHE_DIR", "/tmp/kproxy/")
logger.info(f"Cache storage is at '{cache_dir}'.")
MIN_FILE_SIZE_BYTES = 250 * 1024 * 1024
cache = diskcache.Cache(directory=cache_dir, disk_min_file_size=MIN_FILE_SIZE_BYTES)

_cache_enabled = not os.getenv("KPROXY_CACHE_DISABLE")


def is_cache_enabled():
    return _cache_enabled


def cache_enable(value: bool):
    global _cache_enabled
    _cache_enabled = value


def check_version():
    version = cache.get(VERSION_CACHE_KEY, default=1)
    if version != VERSION:
        cache.clear()
        cache[VERSION_CACHE_KEY] = VERSION
        logger.warning(f"Old cache version! Creating new cache with version: {VERSION}")


def is_cacheable(method, params):
    RPC_WHITELIST = {
        "eth_chainId",
        "eth_call",
        "eth_getTransactionReceipt",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getBalance",
        "eth_getTransactionCount",
    }
    do_cache = False
    # TODO: take in consideration the other string blocks like "pending", "earliest"
    if method in RPC_WHITELIST and "latest" not in params:
        do_cache = True
    if method in {"eth_chainId", "eth_getCode"}:  # TODO: eth_getCode may change for the same block?
        do_cache = True
    return do_cache


def generate_cache_key(value) -> str:
    # Inspired from web3.middleware.cache.generate_cache_key

    if isinstance(value, (bytes, bytearray)):
        return hashlib.md5(value).hexdigest()
    elif isinstance(value, str):
        return generate_cache_key(value.encode("utf-8"))
    elif isinstance(value, (bool, NoneType, numbers.Number)):
        return generate_cache_key(repr(value))
    elif isinstance(value, collections.abc.Mapping):
        return generate_cache_key(((key, value[key]) for key in sorted(value.keys())))
    elif isinstance(value, (collections.abc.Sequence, collections.abc.Generator)):
        return generate_cache_key("".join((generate_cache_key(item) for item in value)))
    else:
        raise TypeError(
            f"Cannot generate cache key for value {value} of type {type(value)}"
        )


check_version()
