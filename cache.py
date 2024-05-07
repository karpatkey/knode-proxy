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
MIN_FILE_SIZE_BYTES = 64 * 1024
EVICTION_POLICY = "least-recently-used"
SIZE_LIMIT = int(os.environ.get("KPROXY_CACHE_SIZE_MB", "250")) * 1024 * 1024

cache_dir = os.environ.get("KPROXY_CACHE_DIR", "/tmp/kproxy/")
logger.info(f"Cache storage is at '{cache_dir}'.")

cache = diskcache.Cache(
    directory=cache_dir, size_limit=SIZE_LIMIT, disk_min_file_size=MIN_FILE_SIZE_BYTES, eviction_policy=EVICTION_POLICY
)

_cache_enabled = not os.getenv("KPROXY_CACHE_DISABLE")

PREDEFINED_BLOCK_PARAMS = {"latest", "safe", "earliest", "pending", "finalized"}


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
        "eth_getBlockByNumber",
    }
    RPC_WITH_BLOCK_NUMBER_IN_LAST_ARG = {
        "eth_call",
        "eth_getTransactionReceipt",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getBlockByNumber",
    }

    do_cache = False
    if method in RPC_WHITELIST:
        if method in RPC_WITH_BLOCK_NUMBER_IN_LAST_ARG and params[-1] in PREDEFINED_BLOCK_PARAMS:
            pass
        elif method == "eth_getLogs" and (params[0] in PREDEFINED_BLOCK_PARAMS or params[1] in PREDEFINED_BLOCK_PARAMS):
            pass
        elif method == "eth_getBlockByNumber" and params[0] in PREDEFINED_BLOCK_PARAMS:
            pass
        else:
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
        raise TypeError(f"Cannot generate cache key for value {value} of type {type(value)}")


def get_rpc_cache_key(blockchain, method, params):
    params_hash = generate_cache_key(params)
    return f"{blockchain}.{method}.{params_hash}"


def get_rpc_response_from_cache(cache_key):
    if is_cache_enabled() and cache_key in cache:
        key, data = cache[cache_key]
        return {"jsonrpc": "2.0", "id": 11, key: data}


def set_rpc_response_to_cache(resp_data, cache_key, method, params):
    if is_cache_enabled() and is_cacheable(method, params):
        if "error" not in resp_data and "result" in resp_data and resp_data["result"] is not None:
            cache[cache_key] = ("result", resp_data["result"])
        elif "error" in resp_data:
            # These errors are "good ones", we want to cache the error because they should be invariant
            # (don't depend on the node nor the time)
            ERRORS_TO_CACHE = {-32000, -32015}
            if resp_data["error"]["code"] in ERRORS_TO_CACHE:
                cache[cache_key] = ("error", resp_data["error"])


def clear():
    cache.clear()


check_version()
