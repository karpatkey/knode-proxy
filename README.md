# knode-proxy

RPC endpoint requests single point of entry and management system.

## Install

Build the docker image and run it. It will listen in http://127.0.0.1:8888

You must configure an upstream HTTPS server (nginx, caddy, etc).

### Config

### Upstream node list

Use one of the following env variables:

`KNODE_CFG='{"nodes": {"ethereum": [https://foo, https://bar], "gnosis": ...}}'`

or

`KNODE_CFG_FILE=path/to/file.json`

#### Authorized keys

To allow only authorized users to use the node you can specify a query string
authorization key.

Pass an env variable with the authorized keys in the following format when running the service:

`KNODE_AUTHORIZED_KEYS=my-secret-key1,my-secret-key2`

Then access the node as foolows:

`https://mynode.com?key=my-secret-key1`
