"""Temporary AWS credentials from an IoT thing certificate (no stored AWS keys)."""
import json
import re
import ssl
from urllib.parse import quote
from urllib.request import Request, urlopen


def temporary_credentials(endpoint, role_alias, thing_name, cert_path, key_path, ca_path):
    if not re.fullmatch(r"[A-Za-z0-9.-]+", endpoint) or ".credentials.iot." not in endpoint:
        raise ValueError("credentials endpoint must be an AWS IoT credentials hostname")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", role_alias):
        raise ValueError("invalid role alias")
    context = ssl.create_default_context(cafile=ca_path)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    url = "https://{}/role-aliases/{}/credentials".format(endpoint, quote(role_alias))
    request = Request(url, headers={"x-amzn-iot-thingname": thing_name})
    with urlopen(request, context=context, timeout=15) as response:
        result = json.load(response)["credentials"]
    return {
        "aws_access_key_id": result["accessKeyId"],
        "aws_secret_access_key": result["secretAccessKey"],
        "aws_session_token": result["sessionToken"],
    }
