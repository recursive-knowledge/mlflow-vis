"""Attach Cloudflare Access service-token headers to MLflow requests.

The tracking server sits behind Cloudflare Access, so an unauthenticated
request gets a login redirect rather than an API response. Access accepts
service tokens only as this specific header pair — there is no bearer-token
equivalent — which is why this hooks MLflow's request path instead of using
MLFLOW_TRACKING_TOKEN.

Registered via the ``mlflow.request_header_provider`` entry point, so it
applies to every REST call MLflow makes with no changes to training code.
"""

import os

from mlflow.tracking.request_header.abstract_request_header_provider import (
    RequestHeaderProvider,
)

CLIENT_ID_ENV = "CF_ACCESS_CLIENT_ID"
CLIENT_SECRET_ENV = "CF_ACCESS_CLIENT_SECRET"


class CloudflareAccessHeaderProvider(RequestHeaderProvider):
    def in_context(self) -> bool:
        # Inert when the tokens are absent, so the same install works against
        # a loopback server reached over an SSH forward.
        return bool(os.environ.get(CLIENT_ID_ENV) and os.environ.get(CLIENT_SECRET_ENV))

    def request_headers(self) -> dict:
        return {
            "CF-Access-Client-Id": os.environ[CLIENT_ID_ENV],
            "CF-Access-Client-Secret": os.environ[CLIENT_SECRET_ENV],
        }
