# knode-proxy

RPC endpoint requests single point of entry and management system.

## Install

Build the docker image and run it. It will listen in http://127.0.0.1:8888

To deploy in production you must configure an upstream HTTPS server (nginx, caddy, etc).

### Config

### Upstream node list

Use one of the following env variables:

`KPROXY_NODE_CFG='{"nodes": {"ethereum": [https://foo, https://bar], "gnosis": ...}}'`

or

`KPROXY_NODE_CFG_FILE=path/to/file.json`

#### Authorized keys

To allow only authorized users to use the node you can specify a query string
authorization key.

Pass an env variable with the authorized keys in the following format when running the service:

`KPROXY_AUTHORIZED_KEYS=my-secret-key1,my-secret-key2`

Then access the node as foolows:

`https://mynode.com/ethereum?key=my-secret-key1`

### Cache

To speed up the responses, and to relieve the upstream servers, knode-proxy caches some responses that
are expected to be constant. For example the balance of a wallet at a specific block, etc.

There are two env variables to control the cache.

`KPROXY_CACHE_DIR` to select where is stored the cache.
When deploying with docker take care to use a volume so the cache is not lost on restarts.

`KPROXY_CACHE_DISABLE` to entirely disable the cache.