# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""
A Web browser powered by Selenium that can be used to run JavaScript tests.
"""

__all__ = [
    "Browser",
]

import json
import time
from enum import Enum
from typing import Any, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait


class Results:
    class Status(Enum):
        SUCCESS = 1
        TIMEOUT = 2

    def __init__(
        self,
        status: Status,
        results: Optional[Any] = None,
        last_test_message: Optional[Any] = None,
    ):
        self.status = status
        self.results = results
        self.last_test_message = last_test_message


class Browser:
    """
    A Web browser powered by Selenium that can be used to run JavaScript tests.

    The tests must save the results to `window.top.test_results` object.
    """

    TIMEOUT = 5000

    def __init__(self):
        firefox_profile = webdriver.FirefoxProfile()
        firefox_profile.set_preference("dom.disable_open_during_load", False)
        firefox_options = webdriver.FirefoxOptions()
        firefox_options.headless = True
        # Results are polled via window.test_results, not page
        # load events, so there is no need to wait for page load.
        firefox_options.set_capability("pageLoadStrategy", "none")
        self.driver = webdriver.Firefox(
            options=firefox_options, firefox_profile=firefox_profile
        )

    def run_tests(
        self,
        uri: str,
        timeout: float = TIMEOUT,
        incremental_timeout: Optional[int] = None,
    ) -> Results:
        """
        Load a page with JavaScript tests return the tests results.

        :param uri: URI of the HTML page containing the tests
        :param timeout: timeout value for the entire test suite (milliseconds)
        :param incremental_timeout: optional timeout value for
            individual tests (milliseconds)
        :return: test results
        """

        self.driver.get(uri)

        start_time = time.perf_counter()
        results = None
        time_left = timeout  # milliseconds
        while time_left > 0:
            try:
                results = self._get_test_results(
                    incremental_timeout or time_left
                )
            except TimeoutException:
                return Results(
                    status=Results.Status.TIMEOUT, last_test_message=results
                )
            if results.get("type") == "complete":
                return Results(
                    status=Results.Status.SUCCESS,
                    results=results,
                )
            time_passed = (time.perf_counter() - start_time) * 1000
            time_left = timeout - time_passed

        return Results(
            status=Results.Status.TIMEOUT, last_test_message=results
        )

    def _get_test_results(self, timeout: float) -> Any:
        """
        Load the test results from the page.

        When found, the results are removed from the page.

        TimeoutException is raised if results could not be fetched within
        the specified timeout.

        :param timeout: timeout in milliseconds
        :return: test results
        """
        timeout = timeout / 1000  # milliseconds to seconds
        results = WebDriverWait(
            self.driver,
            timeout,
            # Adjust the poll frequency based on the timeout value
            poll_frequency=min(0.1, timeout / 10),
        ).until(
            lambda d: d.execute_script(
                """
                let results = window.test_results;
                delete window.test_results;
                return results;
            """
            )
        )
        return json.loads(results)

    def close(self):
        self.driver.quit()
