"""arXiv data science pipeline."""

import ssl

try:
    import certifi

    ssl._create_default_https_context = lambda: ssl.create_default_context(
        cafile=certifi.where()
    )
except ImportError:
    pass

__version__ = "0.1.0"
