from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, AsyncGenerator, Iterable

from parsel import Selector
from pydantic import ValidationError
from typing_extensions import Unpack

from crawlee import EnqueueStrategy
from crawlee._request import BaseRequestData
from crawlee._utils.blocked import RETRY_CSS_SELECTORS
from crawlee._utils.urls import convert_to_absolute_url, is_url_absolute
from crawlee.basic_crawler import BasicCrawler, BasicCrawlerOptions, ContextPipeline
from crawlee.errors import SessionError
from crawlee.http_clients import HttpxHttpClient
from crawlee.http_crawler import HttpCrawlingContext
from crawlee.parsel_crawler._parsel_crawling_context import ParselCrawlingContext

if TYPE_CHECKING:
    from crawlee._types import AddRequestsKwargs, BasicCrawlingContext


class ParselCrawler(BasicCrawler[ParselCrawlingContext]):
    """A crawler that fetches the request URL using `httpx` and parses the result with `Parsel`."""

    def __init__(
        self,
        *,
        additional_http_error_status_codes: Iterable[int] = (),
        ignore_http_error_status_codes: Iterable[int] = (),
        **kwargs: Unpack[BasicCrawlerOptions[ParselCrawlingContext]],
    ) -> None:
        """Initialize the ParselCrawler.

        Args:
            additional_http_error_status_codes: HTTP status codes that should be considered errors (and trigger a retry)

            ignore_http_error_status_codes: HTTP status codes that are normally considered errors but we want to treat
                them as successful

            kwargs: Arguments to be forwarded to the underlying BasicCrawler
        """
        kwargs['_context_pipeline'] = (
            ContextPipeline()
            .compose(self._make_http_request)
            .compose(self._parse_http_response)
            .compose(self._handle_blocked_request)
        )

        kwargs.setdefault(
            'http_client',
            HttpxHttpClient(
                additional_http_error_status_codes=additional_http_error_status_codes,
                ignore_http_error_status_codes=ignore_http_error_status_codes,
            ),
        )

        kwargs.setdefault('_logger', logging.getLogger(__name__))

        super().__init__(**kwargs)

    async def _make_http_request(self, context: BasicCrawlingContext) -> AsyncGenerator[HttpCrawlingContext, None]:
        result = await self._http_client.crawl(
            request=context.request,
            session=context.session,
            proxy_info=context.proxy_info,
            statistics=self._statistics,
        )

        yield HttpCrawlingContext(
            request=context.request,
            session=context.session,
            proxy_info=context.proxy_info,
            add_requests=context.add_requests,
            send_request=context.send_request,
            push_data=context.push_data,
            get_key_value_store=context.get_key_value_store,
            log=context.log,
            http_response=result.http_response,
        )

    async def _handle_blocked_request(
        self, crawling_context: ParselCrawlingContext
    ) -> AsyncGenerator[ParselCrawlingContext, None]:
        if self._retry_on_blocked:
            status_code = crawling_context.http_response.status_code

            if crawling_context.session and crawling_context.session.is_blocked_status_code(status_code=status_code):
                raise SessionError(f'Assuming the session is blocked based on HTTP status code {status_code}')

            parsel = crawling_context.selector

            matched_selectors = [
                selector
                for selector in RETRY_CSS_SELECTORS
                if parsel.type in ('html', 'xml') and parsel.css(selector).get() is not None
            ]

            if matched_selectors:
                raise SessionError(
                    'Assuming the session is blocked - '
                    f"HTTP response matched the following selectors: {'; '.join(matched_selectors)}"
                )

        yield crawling_context

    async def _parse_http_response(
        self,
        context: HttpCrawlingContext,
    ) -> AsyncGenerator[ParselCrawlingContext, None]:
        parsel_selector = await asyncio.to_thread(lambda: Selector(body=context.http_response.read()))

        async def enqueue_links(
            *,
            selector: str = 'a',
            label: str | None = None,
            user_data: dict[str, Any] | None = None,
            **kwargs: Unpack[AddRequestsKwargs],
        ) -> None:
            kwargs.setdefault('strategy', EnqueueStrategy.SAME_HOSTNAME)

            requests = list[BaseRequestData]()
            user_data = user_data or {}

            link: Selector
            for link in parsel_selector.css(selector):
                link_user_data = user_data

                if label is not None:
                    link_user_data.setdefault('label', label)

                if (url := link.xpath('@href').get()) is not None:
                    url = url.strip()

                    if not is_url_absolute(url):
                        url = str(convert_to_absolute_url(context.request.url, url))

                    try:
                        request = BaseRequestData.from_url(url, user_data=link_user_data)
                    except ValidationError as exc:
                        context.log.debug(
                            f'Skipping URL "{url}" due to invalid format: {exc}. '
                            'This may be caused by a malformed URL or unsupported URL scheme. '
                            'Please ensure the URL is correct and retry.'
                        )
                        continue

                    requests.append(request)

            await context.add_requests(requests, **kwargs)

        yield ParselCrawlingContext(
            request=context.request,
            session=context.session,
            proxy_info=context.proxy_info,
            enqueue_links=enqueue_links,
            add_requests=context.add_requests,
            send_request=context.send_request,
            push_data=context.push_data,
            get_key_value_store=context.get_key_value_store,
            log=context.log,
            http_response=context.http_response,
            selector=parsel_selector,
        )
