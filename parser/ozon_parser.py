import json
import logging
import time
import concurrent.futures
from typing import List, Optional
from driver_manager.selenium_manager import SeleniumManager
from models.schemas import ArticleResult, PriceInfo
from utils.helpers import (
    build_ozon_api_url, 
    find_web_price_property, 
    find_product_title,
    parse_price_data,
    is_valid_json_response
)
from config.settings import settings


logger = logging.getLogger(__name__)


class OzonParser:
    def __init__(self):
        self.workers = []
    
    def initialize(self):
        """
        Initialize parser - workers will be created on demand
        """
        logger.info("Ozon parser initialized successfully")
    
    def parse_articles(self, articles: List[int]) -> List[ArticleResult]:
        """
        Parse multiple articles using parallel workers
        """
        worker_groups = self._distribute_articles(articles)
        
        if len(worker_groups) == 1:
            return self._parse_with_single_worker(articles)
        
        return self._parse_with_multiple_workers(worker_groups, articles)
    
    def _distribute_articles(self, articles: List[int]) -> List[List[int]]:
        """
        Distribute articles across workers
        """
        total = len(articles)
        
        if total <= settings.MAX_ARTICLES_PER_WORKER:
            return [articles]
        
        groups = []
        for i in range(0, total, settings.MAX_ARTICLES_PER_WORKER):
            group = articles[i:i + settings.MAX_ARTICLES_PER_WORKER]
            groups.append(group)
            if len(groups) >= settings.MAX_WORKERS:
                break
        
        return groups
    
    def _parse_with_single_worker(self, articles: List[int]) -> List[ArticleResult]:
        """
        Parse with single worker
        """
        worker = OzonWorker()
        try:
            worker.initialize()
            return worker.parse_articles(articles)
        finally:
            worker.close()
    
    def _parse_with_multiple_workers(self, worker_groups: List[List[int]], original_articles: List[int]) -> List[ArticleResult]:
        """
        Parse using multiple workers in parallel
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(worker_groups)) as executor:
            futures = []
            
            for group in worker_groups:
                future = executor.submit(self._parse_worker_group, group)
                futures.append(future)
            
            all_results = []
            for future in concurrent.futures.as_completed(futures):
                worker_results = future.result()
                all_results.extend(worker_results)
        
        return self._sort_results_by_original_order(all_results, original_articles)
    
    def _parse_worker_group(self, articles: List[int]) -> List[ArticleResult]:
        """
        Parse articles with dedicated worker
        """
        worker = OzonWorker()
        try:
            worker.initialize()
            return worker.parse_articles(articles)
        finally:
            worker.close()
    
    def _sort_results_by_original_order(self, results: List[ArticleResult], original_articles: List[int]) -> List[ArticleResult]:
        """
        Sort results to match original article order
        """
        result_dict = {result.article: result for result in results}
        return [result_dict[article] for article in original_articles if article in result_dict]
    
    def close(self):
        """
        Close parser
        """
        logger.info("Parser closed successfully")


class OzonWorker:
    def __init__(self):
        self.selenium_manager = SeleniumManager()
        self.driver = None
    
    def initialize(self):
        """
        Initialize worker with driver setup
        """
        try:
            self.driver = self.selenium_manager.setup_driver()
            logger.info("Worker initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize worker: {e}")
            raise
    
    def parse_articles(self, articles: List[int]) -> List[ArticleResult]:
        """
        Parse articles sequentially
        """
        if not self.driver:
            raise RuntimeError("Worker not initialized")
        
        results = []
        for article in articles:
            result = self.parse_single_article(article)
            results.append(result)
        
        return results
    
    def parse_single_article(self, article: int) -> ArticleResult:
        """
        Parse single article with retries
        """
        for attempt in range(settings.MAX_RETRIES):
            try:
                logger.info(f"Parsing article {article}, attempt {attempt + 1}")
                
                # Build URL
                url = build_ozon_api_url(article)
                logger.info(f"Built URL: {url}")
                
                # Navigate to URL
                navigation_success = self.selenium_manager.navigate_to_url(url)
                logger.info(f"Navigation success: {navigation_success}")
                
                if not navigation_success:
                    logger.warning(f"Failed to navigate to URL for article {article}")
                    
                    # Попробуем получить дополнительную информацию для отладки
                    if self.driver:
                        current_url = self.driver.current_url
                        page_title = self.driver.title
                        logger.info(f"Current URL: {current_url}")
                        logger.info(f"Page title: {page_title}")
                        
                        # Сохраним часть исходного кода для анализа
                        page_source = self.driver.page_source[:1000]
                        logger.debug(f"Page source sample: {page_source}")
                    
                    if attempt < settings.MAX_RETRIES - 1:
                        logger.info(f"Retrying navigation in {settings.RETRY_DELAY} seconds...")
                        time.sleep(settings.RETRY_DELAY)
                        continue
                    else:
                        return ArticleResult(
                            article=article,
                            success=False,
                            error="Failed to navigate to URL"
                        )
                
                # Debug page content first
                self.selenium_manager.debug_page_content()
                
                # Wait for JSON response
                page_source = self.selenium_manager.wait_for_json_response()
                
                if not page_source:
                    logger.warning(f"No JSON response for article {article}")
                    if attempt < settings.MAX_RETRIES - 1:
                        logger.info(f"Retrying JSON wait in {settings.RETRY_DELAY} seconds...")
                        time.sleep(settings.RETRY_DELAY)
                        continue
                    else:
                        return ArticleResult(
                            article=article,
                            success=False,
                            error="No JSON response received"
                        )
                
                # Parse JSON response
                price_info = self.extract_price_info(page_source)
                
                if price_info:
                    logger.info(f"Successfully parsed article {article}")
                    return ArticleResult(
                        article=article,
                        success=True,
                        price_info=price_info
                    )
                else:
                    logger.warning(f"Failed to extract price info for article {article}")
                    if attempt < settings.MAX_RETRIES - 1:
                        logger.info(f"Retrying price extraction in {settings.RETRY_DELAY} seconds...")
                        time.sleep(settings.RETRY_DELAY)
                        continue
                    else:
                        return ArticleResult(
                            article=article,
                            success=False,
                            error="Failed to extract price info"
                        )
                
            except Exception as e:
                logger.error(f"Error parsing article {article}: {e}")
                if attempt < settings.MAX_RETRIES - 1:
                    logger.info(f"Retrying after error in {settings.RETRY_DELAY} seconds...")
                    time.sleep(settings.RETRY_DELAY)
                    continue
                else:
                    return ArticleResult(
                        article=article,
                        success=False,
                        error=str(e)
                    )
        
        return ArticleResult(
            article=article,
            success=False,
            error="Max retries exceeded"
        )
    
    def extract_price_info(self, json_content: str) -> Optional[PriceInfo]:
        """
        Extract price information from JSON content
        """
        try:
            logger.info("Extracting price info from JSON content")
            
            # Проверяем, что это валидный JSON
            if not is_valid_json_response(json_content):
                logger.warning("Invalid JSON content")
                return None
            
            # Парсим JSON
            data = json.loads(json_content)
            
            # Получаем widgetStates
            widget_states = data.get('widgetStates', {})
            
            if not widget_states:
                logger.warning("No widgetStates found in JSON")
                return None
            
            logger.info(f"Found {len(widget_states)} widget states")
            
            # Ищем webPrice свойство
            web_price_value = find_web_price_property(widget_states)
            
            if not web_price_value:
                logger.warning("No webPrice property found in widget states")
                return None
            
            logger.info("Found webPrice property, parsing price data")
            
            # Парсим данные о цене
            price_info = parse_price_data(web_price_value)
            
            if price_info:
                # Ищем название товара
                title = find_product_title(widget_states)
                if title:
                    price_info.title = title
                    logger.info(f"Found product title: {title[:50]}...")
                
                logger.info(f"Successfully extracted price info: {price_info}")
                return price_info
            else:
                logger.warning("Failed to parse price data from webPrice property")
                return None
                
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            logger.debug(f"JSON content preview: {json_content[:500]}")
            return None
        except Exception as e:
            logger.error(f"Error extracting price info: {e}")
            return None
    
    def close(self):
        """
        Close worker and cleanup resources
        """
        if self.selenium_manager:
            self.selenium_manager.close()
        logger.info("Worker closed successfully")