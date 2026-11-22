"""Keep loopback HTTP off proxies and avoid constructing an unused TLS context."""
from urllib.parse import urlparse

def http_options(url):
    parsed=urlparse(url)
    local=parsed.hostname in ("127.0.0.1","localhost","::1")
    options={"trust_env":not local}
    # HTTP has no TLS to verify. HTTPS, including loopback HTTPS, keeps
    # certificate verification enabled.
    if local and parsed.scheme=="http":
        options["verify"]=False
    return options
