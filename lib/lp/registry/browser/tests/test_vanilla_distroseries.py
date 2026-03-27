# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `lp.registry.browser.vanilla_distroseries`."""

from unittest import TestCase

from lp.registry.browser.vanilla_distroseries import Tabs


class FakeRequest(dict):
    """Minimal request stub for Tabs tests."""

    def __init__(self, form=None, query_string=""):
        super().__init__(QUERY_STRING=query_string)
        self.form = form or {}


class TestTabs(TestCase):

    def _makeTabs(self, query_string="", form=None):
        request = FakeRequest(form=form or {}, query_string=query_string)
        return Tabs(
            param="packages-chart",
            default="source",
            tabs=[("source", "Source"), ("binary", "Binary")],
            request=request,
            base_url="/ubuntu/hoary/+vanilla",
            swap_url="/ubuntu/hoary/+vanilla-distroseries-packages-chart",
            swap_target="#packages-chart",
            swap_style="outerHTML",
            aria_label="Package builds",
        )

    def _tabByKey(self, tabs, key):
        for tab in tabs:
            if tab["panel_id"].endswith("-%s-panel" % key):
                return tab
        self.fail("No tab with key %r" % key)

    def test_swap_url_is_clean(self):
        """swap_url is the clean base URL without query params."""
        tabs = self._makeTabs()
        source_tab = self._tabByKey(tabs, "source")
        binary_tab = self._tabByKey(tabs, "binary")
        self.assertEqual(
            "/ubuntu/hoary/+vanilla-distroseries-packages-chart",
            source_tab["swap_url"],
        )
        self.assertEqual(
            "/ubuntu/hoary/+vanilla-distroseries-packages-chart",
            binary_tab["swap_url"],
        )

    def test_swap_url_ignores_query_string(self):
        """swap_url stays clean regardless of request QUERY_STRING."""
        tabs = self._makeTabs(query_string="packages-list=my-uploads")
        source_tab = self._tabByKey(tabs, "source")
        self.assertEqual(
            "/ubuntu/hoary/+vanilla-distroseries-packages-chart",
            source_tab["swap_url"],
        )

    def test_swap_param_key_and_value(self):
        """Each tab has swap_param_key and swap_param_value."""
        tabs = self._makeTabs()
        source_tab = self._tabByKey(tabs, "source")
        binary_tab = self._tabByKey(tabs, "binary")
        self.assertEqual("packages-chart", source_tab["swap_param_key"])
        self.assertEqual("source", source_tab["swap_param_value"])
        self.assertEqual("packages-chart", binary_tab["swap_param_key"])
        self.assertEqual("binary", binary_tab["swap_param_value"])

    def test_is_default(self):
        """Default tab has is_default=True, others False."""
        tabs = self._makeTabs()
        source_tab = self._tabByKey(tabs, "source")
        binary_tab = self._tabByKey(tabs, "binary")
        self.assertTrue(source_tab["is_default"])
        self.assertFalse(binary_tab["is_default"])

    def test_href_includes_param_for_non_default(self):
        """Non-default tab href includes the tab's param."""
        tabs = self._makeTabs()
        binary_tab = self._tabByKey(tabs, "binary")
        self.assertIn("packages-chart=binary", binary_tab["href"])

    def test_href_preserves_other_params(self):
        """href preserves cross-section params for no-JS fallback."""
        tabs = self._makeTabs(query_string="packages-list=my-uploads")
        binary_tab = self._tabByKey(tabs, "binary")
        self.assertIn("packages-list=my-uploads", binary_tab["href"])
        self.assertIn("packages-chart=binary", binary_tab["href"])

    def test_active_returns_default_when_no_form_param(self):
        tabs = self._makeTabs()
        self.assertEqual("source", tabs.active)

    def test_active_returns_form_param(self):
        tabs = self._makeTabs(form={"packages-chart": "binary"})
        self.assertEqual("binary", tabs.active)

    def test_active_panel_id(self):
        tabs = self._makeTabs()
        self.assertEqual("packages-chart-source-panel", tabs.active_panel_id)

    def test_render_includes_swap_attributes(self):
        tabs = self._makeTabs()
        html = tabs.render
        self.assertIn('swap-url="', html)
        self.assertIn('swap-target="#packages-chart"', html)
        self.assertIn('swap-style="outerHTML"', html)
        self.assertIn('swap-param-key="packages-chart"', html)
        self.assertIn('swap-param-value="source"', html)
        self.assertIn('swap-param-value="binary"', html)
        self.assertIn("swap-current", html)

    def test_render_swap_default_on_default_tab_only(self):
        tabs = self._makeTabs()
        html = tabs.render
        # Default tab ("Source") should have swap-default
        self.assertIn('swap-param-value="source" swap-default', html)
        # Non-default tab ("Binary") should NOT have swap-default
        self.assertNotIn('swap-param-value="binary" swap-default', html)
