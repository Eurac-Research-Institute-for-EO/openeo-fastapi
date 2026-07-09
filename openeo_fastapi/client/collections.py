"""Class and model to define the framework and partial application logic for interacting with Collections.

Classes:
    - CollectionRegister: Framework for defining and extending the logic for working with Collections.

Patched to normalize cube:dimensions keys to OpenEO standard names
(x, y, t, bands) so users don't need to know each STAC catalog's
naming conventions.
"""
import logging

import aiohttp
from fastapi import HTTPException

from openeo_fastapi.api.models import Collection, Collections
from openeo_fastapi.api.types import Endpoint, Error
from openeo_fastapi.client.register import EndpointRegister

logger = logging.getLogger(__name__)

# Mapping of non-standard dimension names to OpenEO standard names.
# Built from a survey of 136 collections on stac.eurac.edu.
_DIMENSION_NAME_MAP = {
    # spatial x
    "X": "x",
    "E": "x",
    "Lon": "x",
    "lon": "x",
    "longitude": "x",
    # spatial y
    "Y": "y",
    "N": "y",
    "Lat": "y",
    "lat": "y",
    "latitude": "y",
    # temporal
    "DATE": "t",
    "time": "t",
    # bands
    "band": "bands",
}


def _sanitize_providers(collection_dict):
    """Remove providers with invalid URLs that fail Pydantic validation.

    Some STAC collections have provider URLs that are malformed:
    '//www.asi.it', 'N/A', or URLs with embedded text.
    Rather than trying to fix every variant, drop the url field
    from providers that would fail validation.
    """
    providers = collection_dict.get("providers")
    if not providers or not isinstance(providers, list):
        return collection_dict

    for provider in providers:
        url = provider.get("url")
        if not url:
            continue
        # Fix protocol-relative URLs
        if url.startswith("//"):
            provider["url"] = f"https:{url}"
        # Add scheme if missing
        elif not url.startswith(("http://", "https://")):
            provider.pop("url", None)
        # Remove URLs with spaces or embedded text (e.g. CSV-like values)
        elif " " in url:
            provider.pop("url", None)

    return collection_dict


def _normalize_dimensions(collection_dict):
    """Rename cube:dimensions keys to OpenEO standard names (x, y, t, bands).

    Modifies the dict in place and returns it.
    """
    dims = collection_dict.get("cube:dimensions")
    if not dims or not isinstance(dims, dict):
        return collection_dict

    normalized = {}
    for name, dim in dims.items():
        standard_name = _DIMENSION_NAME_MAP.get(name, name)
        if standard_name != name:
            logger.debug(
                "Collection %s: renaming dimension '%s' -> '%s'",
                collection_dict.get("id", "?"),
                name,
                standard_name,
            )
        normalized[standard_name] = dim

    collection_dict["cube:dimensions"] = normalized
    return collection_dict

COLLECTIONS_ENDPOINTS = [
    Endpoint(
        path="/collections",
        methods=["GET"],
    ),
    Endpoint(
        path="/collections/{collection_id}",
        methods=["GET"],
    ),
    Endpoint(
        path="/collections/{collection_id}/items",
        methods=["GET"],
    ),
    Endpoint(
        path="/collections/{collection_id}/items/{item_id}",
        methods=["GET"],
    ),
]


class CollectionRegister(EndpointRegister):
    """The CollectionRegister to regulate the application logic for the API behaviour.
    """
    
    def __init__(self, settings) -> None:
        """Initialize the CollectionRegister.

        Args:
            settings (AppSettings): The AppSettings that the application will use.
        """
        super().__init__()
        self.endpoints = self._initialize_endpoints()
        self.settings = settings

    def _initialize_endpoints(self) -> list[Endpoint]:
        """Initialize the endpoints for the register.

        Returns:
            list[Endpoint]: The default list of job endpoints which are packaged with the module.
        """
        return COLLECTIONS_ENDPOINTS

    async def _proxy_request(self, path):
        """Proxy the request with aiohttp.

        Args:
            path (str): The path to proxy to the STAC catalogue.

        Raises:
            HTTPException: Raises an exception with relevant status code and descriptive message of failure.

        Returns:
            The response dictionary from the request.
        """
        async with aiohttp.ClientSession() as client:
            async with client.get(self.settings.STAC_API_URL + path) as response:
                resp = await response.json()
                if response.status == 200:
                    return resp

    async def get_collection(self, collection_id):
        """
        Returns Metadata for specific datasetsbased on collection_id (str).
        
        Args:
            collection_id (str): The collection id to request from the proxy.

        Raises:
            HTTPException: Raises an exception with relevant status code and descriptive message of failure.

        Returns:
            Collection: The proxied request returned as a Collection.
        """
        not_found = Error(
                code="NotFound", message=f"Collection {collection_id} not found."
            )

        if (
            not self.settings.STAC_COLLECTIONS_WHITELIST
            or collection_id in self.settings.STAC_COLLECTIONS_WHITELIST
        ):
            path = f"collections/{collection_id}"
            resp = await self._proxy_request(path)

            if resp:
                _sanitize_providers(resp)
                _normalize_dimensions(resp)
                return Collection(**resp)
            raise HTTPException(
                status_code=404,
                detail=not_found
            )
        raise HTTPException(
            status_code=404,
            detail=not_found
        )

    async def get_collections(self):
        """
        Returns Basic metadata for all datasets, following STAC pagination
        to retrieve all collections (not just the first page).

        Raises:
            HTTPException: Raises an exception with relevant status code and descriptive message of failure.

        Returns:
            Collections: The proxied request returned as a Collections object.
        """
        all_collections = []
        path = "collections"

        while path:
            resp = await self._proxy_request(path)
            if not resp:
                break

            all_collections.extend(resp.get("collections", []))

            # Follow the "next" link if present
            next_link = next(
                (link for link in resp.get("links", []) if link.get("rel") == "next"),
                None,
            )
            if next_link and next_link.get("href"):
                # Extract the relative path from the full URL
                href = next_link["href"]
                stac_url = self.settings.STAC_API_URL.rstrip("/")
                if href.startswith(stac_url):
                    path = href[len(stac_url):].lstrip("/")
                else:
                    path = href
            else:
                path = None

        if not all_collections:
            raise HTTPException(
                status_code=404,
                detail=Error(code="NotFound", message="No Collections found."),
            )

        collections_list = [
            _normalize_dimensions(_sanitize_providers(collection))
            for collection in all_collections
            if (
                not self.settings.STAC_COLLECTIONS_WHITELIST
                or collection["id"] in self.settings.STAC_COLLECTIONS_WHITELIST
            )
        ]

        return Collections(collections=collections_list, links=[])

    async def get_collection_items(self, collection_id):
        """
        Returns Basic metadata for all datasets.
        
        Args:
            collection_id (str): The collection id to request from the proxy.

        Raises:
            HTTPException: Raises an exception with relevant status code and descriptive message of failure.

        Returns:
            The direct response from the request to the stac catalogue.
        """
        not_found = HTTPException(
            status_code=404,
            detail=Error(
                code="NotFound", message=f"Collection {collection_id} not found."
            ),
        )

        if (
            not self.settings.STAC_COLLECTIONS_WHITELIST
            or collection_id in self.settings.STAC_COLLECTIONS_WHITELIST
        ):
            path = f"collections/{collection_id}/items"
            resp = await self._proxy_request(path)

            if resp:
                return resp
            raise not_found
        raise not_found

    async def get_collection_item(self, collection_id, item_id):
        """
        Returns Basic metadata for all datasets
        
        Args:
            collection_id (str): The collection id to request from the proxy.
            item_id (str): The item id to request from the proxy.

        Raises:
            HTTPException: Raises an exception with relevant status code and descriptive message of failure.

        Returns:
            The direct response from the request to the stac catalogue.
        """
        not_found = HTTPException(
            status_code=404,
            detail=Error(
                code="NotFound",
                message=f"Item {item_id} not found in collection {collection_id}.",
            ),
        )

        if (
            not self.settings.STAC_COLLECTIONS_WHITELIST
            or collection_id in self.settings.STAC_COLLECTIONS_WHITELIST
        ):
            path = f"collections/{collection_id}/items/{item_id}"
            resp = await self._proxy_request(path)

            if resp:
                return resp
            raise not_found
        raise not_found
